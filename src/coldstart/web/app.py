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

from coldstart.db import (
    JOB_STATES,
    clear_job_state,
    connection,
    get_digest_jobs,
    readonly_connection,
    set_job_state,
)
from coldstart.logging_setup import get_logger
from coldstart.settings import Settings
from coldstart.web import queries

logger = get_logger(__name__)

_STATIC_DIR = Path(__file__).resolve().parent / "static"

# Module-level so it isn't a call in a default argument (ruff B008).
_BODY = Body(...)

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
        score_band=ScoreBand.STRONG if score >= 70 else ScoreBand.CONSIDER,
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

    @app.get("/api/metrics")
    def api_metrics() -> dict:
        result = _snapshot(lambda conn: queries.metrics(conn, settings))
        return result or {"db_ready": False}

    @app.post("/api/jobs/{global_id}/state")
    def set_state(global_id: str, request: Request, payload: dict = _BODY) -> dict:
        """Mark a job applied, or clear the mark.

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

    def _preview_sections():
        """Real jobs where there are any, a representative sample otherwise —
        a preview of an empty digest teaches you nothing about the layout."""
        from coldstart.digest import DigestSections, build_digest_sections

        jobs = _snapshot(
            lambda conn: get_digest_jobs(conn, since=datetime(2000, 1, 1, tzinfo=UTC))
        )
        if not jobs:
            return DigestSections(
                strong=[_sample_job(94, "Senior Software Engineer", "Northwind")],
                consider=[_sample_job(64, "Backend Engineer", "Contoso", link=None)],
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
