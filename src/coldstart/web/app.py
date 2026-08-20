"""FastAPI app + the thread that serves it inside the daemon process."""

from __future__ import annotations

import asyncio
import socket
import threading
from pathlib import Path

import uvicorn
from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from coldstart.db import readonly_connection
from coldstart.logging_setup import get_logger
from coldstart.settings import Settings
from coldstart.web import queries

logger = get_logger(__name__)

_STATIC_DIR = Path(__file__).resolve().parent / "static"

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
