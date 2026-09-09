"""FastAPI app + the thread that serves it inside the daemon process."""

from __future__ import annotations

import asyncio
import socket
import sqlite3
import threading
from datetime import UTC, datetime, timedelta
from html import escape
from pathlib import Path

import uvicorn
from fastapi import Body, FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from coldstart import ats_control
from coldstart.db import (
    JOB_STATES,
    clear_job_state,
    clear_job_states,
    clear_slice_state,
    connection,
    get_digest_jobs,
    readonly_connection,
    set_job_state,
    set_job_states,
)
from coldstart.logging_setup import get_logger
from coldstart.settings import Settings
from coldstart.web import analytics as analytics_queries
from coldstart.web import queries

logger = get_logger(__name__)

_STATIC_DIR = Path(__file__).resolve().parent / "static"

# Module-level so it isn't a call in a default argument (ruff B008).
_BODY = Body(...)

# A whole company is a few hundred rows at most, so anything past this is a
# stale tab or a bug rather than a decision someone made.
_BULK_STATE_LIMIT = 2000

# How often the SSE loop re-checks for changes. Because upsert_job commits per
# job, this makes rows appear on the page *during* a poll, not after it.
_EVENT_POLL_SECONDS = 3.0


class _RevalidatingStatics(StaticFiles):
    """Serve the page assets with `no-cache`.

    There is no build step and therefore no content hash in the filenames, so
    a cached app.js survives an upgrade and the operator sees stale behaviour
    with no clue why. `no-cache` means "revalidate", not "don't cache" — the
    ETag StaticFiles already sends turns the check into a cheap 304."""

    def is_not_modified(self, response_headers, request_headers) -> bool:
        response_headers["cache-control"] = "no-cache"
        return super().is_not_modified(response_headers, request_headers)

    def file_response(self, *args, **kwargs):
        response = super().file_response(*args, **kwargs)
        response.headers["cache-control"] = "no-cache"
        return response


def _sample_job(score: int, title: str, company: str, link: str | None = "https://example.com/apply"):
    from coldstart.models import (
        EligibilityFlag,
        JobRecord,
        JobStatus,
        LocationFlag,
        ResumeId,
        ScoreBand,
    )

    now = datetime.now(UTC)
    return JobRecord(
        global_id=f"sample:{score}",
        requisition_id="REQ-1",
        company=company,
        title=title,
        location="Austin, TX",
        apply_url=link,
        ats_type="greenhouse",
        posted_at=now - timedelta(days=3),
        resume_used=ResumeId.A,
        score=score,
        score_band=ScoreBand.STRONG if score >= 80 else ScoreBand.REJECT,
        eligible=True,
        matched_skills=["Python", "FastAPI", "PostgreSQL", "AWS"],
        missing_skills=["Kubernetes"],
        reasoning="Sample row, shown because the database has no scored jobs yet.",
        status=JobStatus.SCORED,
        location_flag=LocationFlag.ACCEPTED,
        eligibility_flag=EligibilityFlag.PASSED,
        provider_used="deepseek",
        first_seen_at=now,
        scored_at=now,
    )


def create_app(settings: Settings) -> FastAPI:
    app = FastAPI(title="Coldstart", docs_url=None, redoc_url=None)
    app.mount("/static", _RevalidatingStatics(directory=_STATIC_DIR), name="static")

    def _snapshot(fn):
        """Run `fn(conn)` on a fresh read-only connection.

        One connection per request, never shared — sqlite3 defaults to
        check_same_thread=True and FastAPI runs sync endpoints in a threadpool.
        A missing database file is an empty dashboard, not a 500."""
        try:
            with readonly_connection(settings.db_path) as conn:
                return fn(conn)
        except FileNotFoundError:
            return None
        except sqlite3.OperationalError:
            # A database written before an additive column was added. Read-only
            # connections cannot ALTER, so the fix is to run the pipeline (or
            # scripts/init_db.py) once — until then this is an empty dashboard
            # rather than a 500.
            logger.warning("dashboard query failed — database may need init_schema", exc_info=True)
            return None

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(
            _STATIC_DIR / "index.html", headers={"Cache-Control": "no-cache"}
        )

    @app.get("/healthz")
    def healthz() -> dict:
        return {"ok": True}

    @app.get("/api/jobs")
    def api_jobs(include_reject: bool = Query(default=False)) -> dict:
        jobs = _snapshot(
            lambda conn: queries.list_jobs(conn, settings, include_reject=include_reject)
        )
        return {"jobs": jobs or [], "db_ready": jobs is not None}

    @app.get("/api/jobs/held-back")
    def api_held_back() -> dict:
        """Jobs the location filter held back — never scored, never emailed."""
        jobs = _snapshot(lambda conn: queries.list_location_excluded(conn, settings))
        return {"jobs": jobs or [], "db_ready": jobs is not None}

    @app.get("/api/jobs/stack-excluded")
    def api_stack_excluded() -> dict:
        """Jobs the stack/experience filter held back — never scored, never emailed."""
        jobs = _snapshot(lambda conn: queries.list_stack_excluded(conn, settings))
        return {"jobs": jobs or [], "db_ready": jobs is not None}

    @app.get("/analytics")
    def analytics_page() -> FileResponse:
        return FileResponse(
            _STATIC_DIR / "analytics.html", headers={"Cache-Control": "no-cache"}
        )

    @app.get("/api/analytics")
    def api_analytics() -> dict:
        """Every number the pipeline knows about itself, in one snapshot.

        One endpoint rather than a dozen because the page is read whole and
        the figures have to describe the same instant — see analytics.py.

        The daemon's state is merged in here rather than inside the query
        module: it lives in this process's memory, not in the database, so a
        read-only connection cannot reach it. That also keeps analytics.py
        pure-SQL and testable without a running daemon."""
        from coldstart.daemon import get_state

        payload = _snapshot(lambda conn: analytics_queries.analytics(conn, settings))
        if payload is None:
            return {"db_ready": False}

        state = get_state()
        payload["db_ready"] = True
        payload["daemon"] = None if state is None else state.model_dump(mode="json")
        return payload

    # The actions the queue panel can take. Kept as one endpoint with a
    # named action rather than five verbs, matching /api/jobs/{id}/state.
    _QUEUE_ACTIONS = {
        "run_next": ats_control.run_next,
        "hold": ats_control.hold,
        "release": ats_control.release,
        "clear_priority": ats_control.clear_priority,
    }

    def _require_action_header(request: Request) -> None:
        """Same CSRF guard as the other writes, same reason: the server binds
        to loopback and has no auth, so a custom header is what stops a page
        you happen to have open from POSTing here."""
        if request.headers.get("x-coldstart-action") != "1":
            raise HTTPException(status_code=403, detail="missing X-Coldstart-Action header")

    def _known_sources() -> set[str]:
        """Sources this system actually knows about.

        An unrecognised name must never reach the control row — a typo would
        otherwise sit in the priority list forever, matching nothing and
        quietly implying the queue is ordered when it isn't."""
        known = _snapshot(
            lambda conn: set(analytics_queries.relevant_sources(conn))
            | {row["ats_type"] for row in conn.execute("SELECT ats_type FROM slice_state")}
        )
        return known or set()

    @app.post("/api/queue/{ats_type}")
    def queue_action(ats_type: str, request: Request, payload: dict = _BODY) -> dict:
        """Reorder, hold, release or re-run one source.

        Writes to `daemon_state`, which is the only channel the web layer and
        the run_poll subprocess share (see ats_control.py). A poll already
        running picks this up on its own — it re-reads the order before every
        slice and between jobs — so this takes effect immediately rather than
        at the next cycle."""
        _require_action_header(request)

        action = payload.get("action")
        if action not in _QUEUE_ACTIONS and action != "rerun":
            raise HTTPException(
                status_code=422,
                detail=f"unknown action {action!r}; expected one of "
                f"{sorted([*_QUEUE_ACTIONS, 'rerun'])}",
            )
        if ats_type not in _known_sources():
            raise HTTPException(status_code=404, detail=f"no such source {ats_type!r}")

        try:
            with connection(settings.db_path) as conn:
                if action == "rerun":
                    # Forget the change-detection row so the source counts as
                    # outstanding again, then put it at the front.
                    cleared = clear_slice_state(conn, ats_type)
                    control = ats_control.run_next(conn, ats_type)
                    result = {"rerun": cleared}
                else:
                    control = _QUEUE_ACTIONS[action](conn, ats_type)
                    result = {}
        except sqlite3.OperationalError as exc:
            logger.warning("could not write queue control for %s: %s", ats_type, exc)
            raise HTTPException(status_code=503, detail="database busy, try again") from exc

        # Nudge an idle daemon rather than leaving the choice sitting there for
        # another 29 minutes. Only for actions that create work; holding one
        # source is not a reason to start polling.
        if action in ("run_next", "release", "rerun"):
            from coldstart.daemon import request_poll_soon

            request_poll_soon()

        logger.info("queue: %s %s", ats_type, action)
        return {"ats_type": ats_type, "action": action, **result,
                "control": control.model_dump(mode="json")}

    @app.post("/api/queue")
    def queue_reset(request: Request, payload: dict = _BODY) -> dict:
        """Drop every override and go back to the manifest's own order."""
        _require_action_header(request)
        if payload.get("action") != "reset":
            raise HTTPException(status_code=422, detail="expected {\"action\": \"reset\"}")

        try:
            with connection(settings.db_path) as conn:
                control = ats_control.reset(conn)
        except sqlite3.OperationalError as exc:
            raise HTTPException(status_code=503, detail="database busy, try again") from exc

        from coldstart.daemon import request_poll_soon

        request_poll_soon()
        return {"action": "reset", "control": control.model_dump(mode="json")}

    @app.get("/api/metrics")
    def api_metrics() -> dict:
        result = _snapshot(lambda conn: queries.metrics(conn, settings))
        return result or {"db_ready": False}

    @app.post("/api/jobs/{global_id}/state")
    def set_state(global_id: str, request: Request, payload: dict = _BODY) -> dict:
        """Mark a job applied or declined, or clear the mark.

        The only write the dashboard can perform. Everything else opens a
        `mode=ro` connection; this one takes a normal connection, touches
        exactly one table, and rejects any state not in JOB_STATES — so the
        blast radius stays one row of your own decisions.

        The custom-header requirement is a CSRF guard. The server binds to
        loopback and has no auth, so without it any page you happened to have
        open could POST here; a cross-origin form can't set custom headers,
        and a scripted fetch that does gets stopped at the preflight."""
        if request.headers.get("x-coldstart-action") != "1":
            raise HTTPException(status_code=403, detail="missing X-Coldstart-Action header")

        state = payload.get("state")
        if state is not None and state not in JOB_STATES:
            raise HTTPException(
                status_code=422,
                detail=f"unknown state {state!r}; expected one of {sorted(JOB_STATES)}",
            )

        try:
            with connection(settings.db_path) as conn:
                exists = conn.execute(
                    "SELECT 1 FROM jobs WHERE global_id = ?", (global_id,)
                ).fetchone()
                if exists is None:
                    raise HTTPException(status_code=404, detail="no such job")
                if state is None:
                    clear_job_state(conn, global_id)
                else:
                    set_job_state(conn, global_id, state)
        except sqlite3.OperationalError as exc:
            # A poll holds a write lock only briefly (it commits per job), so
            # this is rare — but say so plainly rather than failing silently.
            logger.warning("could not write job state for %s: %s", global_id, exc)
            raise HTTPException(status_code=503, detail="database busy, try again") from exc

        logger.info("job %s marked %s", global_id, state or "unmarked")
        return {"global_id": global_id, "state": state}

    @app.post("/api/jobs/state")
    def set_states(request: Request, payload: dict = _BODY) -> dict:
        """The same mark, applied to a whole group at once.

        Declining a company from its group header is one decision about many
        rows, so it is one request and one transaction — looping the per-job
        route would leave the company half declined if anything failed
        part-way, and would take 117 round trips to say one thing.

        Same guards as the single-job route, plus a cap: this is still only
        able to touch `job_state`, still rejects any state not in JOB_STATES,
        and now also refuses a batch large enough to be a mistake rather than
        an intention."""
        if request.headers.get("x-coldstart-action") != "1":
            raise HTTPException(status_code=403, detail="missing X-Coldstart-Action header")

        state = payload.get("state")
        if state is not None and state not in JOB_STATES:
            raise HTTPException(
                status_code=422,
                detail=f"unknown state {state!r}; expected one of {sorted(JOB_STATES)}",
            )

        global_ids = payload.get("global_ids")
        if not isinstance(global_ids, list) or not all(isinstance(i, str) for i in global_ids):
            raise HTTPException(status_code=422, detail="global_ids must be a list of strings")
        if not global_ids:
            raise HTTPException(status_code=422, detail="global_ids is empty")
        if len(global_ids) > _BULK_STATE_LIMIT:
            raise HTTPException(
                status_code=422,
                detail=f"too many ids ({len(global_ids)}); the cap is {_BULK_STATE_LIMIT}",
            )

        try:
            with connection(settings.db_path) as conn:
                # Silently skipping unknown ids would let a stale tab report
                # success for rows that no longer exist, so the count comes
                # back and the caller can see the difference.
                placeholders = ",".join("?" * len(global_ids))
                known = [
                    row["global_id"]
                    for row in conn.execute(
                        f"SELECT global_id FROM jobs WHERE global_id IN ({placeholders})",
                        global_ids,
                    )
                ]
                if not known:
                    raise HTTPException(status_code=404, detail="no such jobs")
                if state is None:
                    updated = clear_job_states(conn, known)
                else:
                    updated = set_job_states(conn, known, state)
        except sqlite3.OperationalError as exc:
            logger.warning("could not write %d job state(s): %s", len(global_ids), exc)
            raise HTTPException(status_code=503, detail="database busy, try again") from exc

        logger.info("%d job(s) marked %s", updated, state or "unmarked")
        return {"updated": updated, "requested": len(global_ids), "state": state}

    @app.get("/api/status")
    def api_status() -> dict:
        # Imported here rather than at module scope: daemon.py imports this
        # module to start the server, so a top-level import would be circular.
        from coldstart.daemon import get_state

        state = get_state()
        return {
            "daemon": None if state is None else state.model_dump(mode="json"),
            "data_version": _snapshot(queries.data_version),
        }

    @app.post("/api/daemon/pause")
    def daemon_pause(request: Request) -> dict:
        """Stop the daemon from starting its next poll cycle.

        Same CSRF guard as /api/jobs/{id}/state, same reason (loopback,
        no auth): a cross-origin request can't set a custom header."""
        if request.headers.get("x-coldstart-action") != "1":
            raise HTTPException(status_code=403, detail="missing X-Coldstart-Action header")

        from coldstart.daemon import pause_polling

        state = pause_polling()
        if state is None:
            raise HTTPException(status_code=409, detail="no daemon attached")
        logger.info("polling paused from the dashboard")
        return {"manually_paused": True}

    @app.post("/api/daemon/resume")
    def daemon_resume(request: Request) -> dict:
        if request.headers.get("x-coldstart-action") != "1":
            raise HTTPException(status_code=403, detail="missing X-Coldstart-Action header")

        from coldstart.daemon import resume_polling

        state = resume_polling()
        if state is None:
            raise HTTPException(status_code=409, detail="no daemon attached")
        logger.info("polling resumed from the dashboard")
        return {"manually_paused": False}

    def _preview_sections():
        """Real jobs where there are any, a representative sample otherwise —
        a preview of an empty digest teaches you nothing about the layout."""
        from coldstart.digest import DigestSections, build_digest_sections

        jobs = _snapshot(
            lambda conn: get_digest_jobs(conn, since=datetime(2000, 1, 1, tzinfo=UTC))
        )
        if not jobs:
            return DigestSections(
                strong=[
                    _sample_job(94, "Senior Software Engineer", "Northwind"),
                    _sample_job(88, "Backend Engineer", "Contoso", link=None),
                ],
                fetched_count=33186,
                filtered_count=412,
                scored_count=2,
                failed_count=0,
                spend_today_usd=0.0,
                providers_used=["deepseek"],
                csv_path="output/scored_sample.csv",
                unresolved_errors_count=0,
                window_start=datetime.now(UTC) - timedelta(hours=19),
            )
        return build_digest_sections(
            jobs[:25],
            settings,
            fetched_count=33186,
            filtered_count=412,
            scored_count=len(jobs),
            failed_count=0,
            spend_today_usd=0.0,
            providers_used=["deepseek"],
            csv_path="output/scored_preview.csv",
            unresolved_errors_count=0,
            window_start=datetime.now(UTC) - timedelta(hours=19),
        )

    @app.get("/preview/email")
    def preview_email() -> FileResponse:
        return FileResponse(
            _STATIC_DIR / "preview.html", headers={"Cache-Control": "no-cache"}
        )

    @app.get("/preview/email/render")
    def preview_email_render(fmt: str = Query(default="html")) -> Response:
        """The rendered digest itself, straight from config/email/.

        Errors are shown rather than swallowed: this is where you find out
        you mistyped a template, instead of at 08:00 tomorrow."""
        from coldstart.digest import render_digest_text
        from coldstart.email_template import HTML_TEMPLATE, TemplateProblem
        from coldstart.email_template import render as render_template

        sections = _preview_sections()
        today = datetime.now(UTC).date()

        if fmt == "text":
            return Response(render_digest_text(sections, today), media_type="text/plain")
        try:
            body = render_template(HTML_TEMPLATE, sections=sections, run_date=today)
        except TemplateProblem as exc:
            body = (
                "<body style=\"font:14px ui-monospace,monospace;padding:24px;"
                "background:#fff5f5;color:#991b1b;\">"
                "<b>Template error — the digest would fall back to the built-in layout.</b>"
                f"<pre style=\"white-space:pre-wrap;margin-top:12px;\">{escape(str(exc))}</pre>"
                "</body>"
            )
        return Response(body, media_type="text/html", headers={"Cache-Control": "no-cache"})

    @app.get("/api/preview/version")
    def preview_version() -> dict:
        """Changes when any file in config/email/ is saved — the preview page
        polls this and reloads itself, so editing feels live."""
        from coldstart.email_template import template_version

        return {"version": template_version()}

    @app.get("/api/events")
    async def api_events(request: Request) -> StreamingResponse:
        """Push a token whenever the data or the daemon's state changed.

        Deliberately not a busy poll from the browser: the client refetches
        only when this says something actually moved."""

        async def stream():
            from coldstart.daemon import get_state

            last: tuple | None = None
            while not await request.is_disconnected():
                state = get_state()
                token = (
                    _snapshot(queries.data_version),
                    None if state is None else state.version,
                )
                if token != last:
                    last = token
                    yield f"data: {token[0]}|{token[1]}\n\n"
                else:
                    yield ": keepalive\n\n"
                await asyncio.sleep(_EVENT_POLL_SECONDS)

        return StreamingResponse(
            stream(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    return app


class DashboardServer:
    def __init__(self, server: uvicorn.Server, thread: threading.Thread, url: str) -> None:
        self._server = server
        self._thread = thread
        self.url = url

    def stop(self) -> None:
        self._server.should_exit = True
        self._thread.join(timeout=10)


def serve_in_thread(settings: Settings) -> DashboardServer:
    """Start uvicorn on a background thread.

    The socket is bound here, on the calling thread, so "port already in use"
    surfaces as a clean OSError the daemon can report — rather than uvicorn
    calling sys.exit inside a thread where nobody sees it.

    uvicorn skips its own signal handling off the main thread, so the daemon's
    SIGINT/SIGTERM handlers stay installed."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind((settings.dashboard_host, settings.dashboard_port))
    except OSError as exc:
        sock.close()
        raise OSError(
            f"cannot bind the dashboard to {settings.dashboard_host}:{settings.dashboard_port} "
            f"({exc}). Set DASHBOARD_PORT to a free port, or DASHBOARD_ENABLED=false."
        ) from exc
    sock.listen(128)

    config = uvicorn.Config(
        create_app(settings),
        log_level=settings.log_level.lower(),
        access_log=False,
        lifespan="off",
        timeout_graceful_shutdown=2,
    )
    server = uvicorn.Server(config)

    thread = threading.Thread(
        target=server.run, kwargs={"sockets": [sock]}, name="coldstart-dashboard", daemon=True
    )
    thread.start()

    url = f"http://{settings.dashboard_host}:{settings.dashboard_port}"
    logger.info("dashboard serving at %s", url)
    return DashboardServer(server, thread, url)
