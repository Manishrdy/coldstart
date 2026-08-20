"""Module 20 — the daemon that owns the clock.

Everything the pipeline does was already implemented; nothing ever ran it.
scope.md §3.2 specifies a 30-minute manifest poll with per-slice sha256
comparison, and DEVELOPMENT_PLAN.md Module 5 reasons about "most of the 48
daily polls" — but `poll_interval_minutes` and `digest_time_pdt` were read by
nothing at all. This module is what finally executes that design.

Two deliberate shapes worth knowing before editing:

1. **Subprocesses, not function calls.** run_poll is spawned as a child
   process rather than imported. A single parquet slice peaks around 10 GB
   RSS (workday: 839k rows, 4.13 GB uncompressed, 96% of it description+raw),
   and that memory has to go back to the OS between cycles. Process exit is
   the only reliable way. It also disposes of routing.py's unbounded
   `_route_cache`, which would otherwise grow for the daemon's whole lifetime.

2. **A short tick, not a long sleep.** The loop wakes every 15 seconds and
   compares wall clocks, rather than sleeping for the poll interval. That is
   what satisfies scope.md §12's "should not assume or require the machine to
   stay awake" — after a laptop sleep, `now >= next_poll_at` is simply already
   true and the missed work runs once, not once per interval skipped.
"""

from __future__ import annotations

import fcntl
import logging
import os
import re
import signal
import subprocess
import sys
import threading
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import FrameType
from typing import Literal
from zoneinfo import ZoneInfo

import httpx
from pydantic import BaseModel

from coldstart import exit_codes
from coldstart.budget import local_day_bounds_utc
from coldstart.db import (
    connection,
    digest_sent_today,
    get_daemon_state,
    init_schema,
    set_daemon_state,
)
from coldstart.errors import log_error
from coldstart.logging_setup import get_logger
from coldstart.settings import Settings

logger = get_logger(__name__)

_SOURCE_FILE = "daemon.py"

_REPO_ROOT = Path(__file__).resolve().parent.parent.parent
_POLL_SCRIPT = _REPO_ROOT / "scripts" / "run_poll.py"
_DIGEST_SCRIPT = _REPO_ROOT / "scripts" / "run_digest.py"

# Short enough that a Ctrl-C feels immediate and a wake-from-sleep is noticed
# promptly; long enough that an idle daemon is invisible in `top`.
_TICK_SECONDS = 15

_LOCK_FILENAME = "coldstart.lock"
_MANIFEST_ETAG_KEY = "manifest_etag"

Activity = Literal["idle", "checking_upstream", "polling", "digesting"]


class AlreadyRunning(Exception):
    """Another daemon already holds the lock file."""

    def __init__(self, holder: str) -> None:
        self.holder = holder
        super().__init__(
            f"another coldstart daemon is already running (pid {holder}). "
            "Stop it first, or run the one-shot scripts directly."
        )


class DaemonState(BaseModel):
    """What the daemon is doing right now, for the dashboard's /api/status.

    Held in memory and mutated under `_state_lock`; `get_state()` hands out a
    deep copy so the web threadpool never reads a half-written object."""

    started_at: datetime
    activity: Activity = "idle"

    last_upstream_check_at: datetime | None = None
    last_upstream_change_at: datetime | None = None
    upstream_etag: str | None = None

    last_poll_started_at: datetime | None = None
    last_poll_finished_at: datetime | None = None
    last_poll_exit_code: int | None = None
    last_poll_summary: str | None = None

    next_poll_at: datetime
    next_digest_at: datetime
    digest_sent_today: bool = False
    budget_paused_until: datetime | None = None
    consecutive_poll_failures: int = 0

    # Bumped on every mutation. The SSE endpoint watches this so a status
    # change pushes to the page even when no new job rows landed.
    version: int = 0


_state: DaemonState | None = None
_state_lock = threading.Lock()

_child_lock = threading.Lock()
_child: subprocess.Popen[str] | None = None


def get_state() -> DaemonState | None:
    """Thread-safe snapshot for the web layer. None before run_daemon starts."""
    with _state_lock:
        return None if _state is None else _state.model_copy(deep=True)


def _set_state(state: DaemonState) -> None:
    global _state
    with _state_lock:
        _state = state


def _update(**fields: object) -> DaemonState:
    global _state
    with _state_lock:
        if _state is None:
            raise RuntimeError("daemon state not initialised")
        for key, value in fields.items():
            setattr(_state, key, value)
        _state.version += 1
        return _state.model_copy(deep=True)


# --- process hygiene -------------------------------------------------------


def acquire_singleton_lock(data_dir: Path):
    """Take an exclusive advisory lock so a second daemon can't double-poll.

    Nothing prevented two overlapping run_poll processes before this — WAL
    plus busy_timeout was the only protection, and it protects the database,
    not the LLM spend. flock is released by the kernel when the process dies,
    so a crash never leaves a stale lock behind (unlike a bare pidfile)."""
    lock_path = Path(data_dir) / _LOCK_FILENAME
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError as exc:
        handle.seek(0)
        holder = handle.read().strip() or "unknown"
        handle.close()
        raise AlreadyRunning(holder) from exc

    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


def sweep_partial_downloads(data_dir: Path) -> int:
    """Delete `.part` files left behind by a download that died mid-stream.

    fetcher._stream_to_disk writes to `<name>.parquet.part` and renames on
    success; nothing has ever cleaned up the failures. Harmless in a one-shot
    script, but a daemon accumulates them indefinitely."""
    removed = 0
    for stale in Path(data_dir).glob("*.part"):
        try:
            stale.unlink()
            removed += 1
        except OSError as exc:  # pragma: no cover - filesystem-dependent
            logger.warning("could not remove stale partial download %s: %s", stale, exc)
    if removed:
        logger.info("removed %d stale partial download(s) from %s", removed, data_dir)
    return removed


def _terminate_child() -> None:
    with _child_lock:
        proc = _child
    if proc is not None and proc.poll() is None:
        logger.warning("terminating child process %s", proc.pid)
        proc.terminate()


def install_signal_handlers(stop: threading.Event) -> None:
    def handler(signum: int, _frame: FrameType | None) -> None:
        name = signal.Signals(signum).name
        if stop.is_set():
            logger.warning("%s again — terminating the running child now", name)
            _terminate_child()
            return
        logger.info("%s received — finishing the current step, then shutting down", name)
        stop.set()
        _terminate_child()

    signal.signal(signal.SIGINT, handler)
    signal.signal(signal.SIGTERM, handler)


# --- upstream change detection --------------------------------------------


def upstream_changed(settings: Settings, conn) -> bool:
    """Conditional GET against the manifest — the cheap 30-minute check.

    This is a pre-filter, NOT the source of truth. When it says "changed" we
    spawn run_poll, which does the authoritative per-slice sha256 comparison
    scope.md §3.2 specifies and early-returns for free if nothing really moved.
    So a false positive costs one cheap subprocess; a 304 costs one HTTP round
    trip with no body at all.

    The live manifest serves a strong ETag and has (as of this writing) not
    regenerated since 2026-08-07, so in practice this returns False nearly
    every time."""
    etag = get_daemon_state(conn, _MANIFEST_ETAG_KEY)
    headers = {"If-None-Match": etag} if etag else {}

    response = httpx.get(
        settings.manifest_url, headers=headers, timeout=30.0, follow_redirects=True
    )
    if response.status_code == 304:
        logger.info("upstream unchanged (304 Not Modified)")
        return False
    response.raise_for_status()

    new_etag = response.headers.get("etag")
    if new_etag:
        set_daemon_state(conn, _MANIFEST_ETAG_KEY, new_etag)
        _update(upstream_etag=new_etag)

    if etag is not None and new_etag == etag:
        logger.info("upstream unchanged (same etag %s)", etag)
        return False

    if new_etag is None:
        # No ETag from the server means we can't tell. Fall through to run_poll
        # and let the per-slice sha256 comparison be the judge — the
        # correct-but-slower path, never a silently skipped update.
        logger.info("upstream sent no ETag — polling and letting sha256 decide")
    else:
        logger.info("upstream changed (etag %s -> %s)", etag, new_etag)
    return True


def forced_poll_due(state: DaemonState, settings: Settings, now: datetime) -> bool:
    """Poll anyway every FORCE_POLL_HOURS, and always on the first tick.

    An ETag can lie — a CDN can serve a stale one, and a previous run that
    crashed mid-slice leaves slice_state behind with the manifest unchanged.
    run_poll is cheap when there is genuinely nothing to do, so a periodic
    unconditional pass is a much better trade than trusting one header."""
    if state.last_poll_started_at is None:
        return True
    return now - state.last_poll_started_at >= timedelta(hours=settings.force_poll_hours)


# --- schedule arithmetic ---------------------------------------------------


def _digest_time(settings: Settings) -> tuple[int, int]:
    hours, minutes = settings.digest_time_pdt.split(":")
    return int(hours), int(minutes)


def next_digest_at(settings: Settings, now: datetime) -> datetime:
    """The next wall-clock moment the digest is due, in UTC (display only)."""
    zone = local_day_bounds_utc  # noqa: F841 - see note below
    tz = ZoneInfo(settings.timezone)
    local_now = now.astimezone(tz)
    hour, minute = _digest_time(settings)
    candidate = local_now.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if candidate <= local_now:
        candidate += timedelta(days=1)
    return candidate.astimezone(UTC)


def next_local_midnight(tz: str) -> datetime:
    """Start of the next local day, in UTC — reuses budget.py's day bounds so
    the budget pause lifts at exactly the moment today_spend's window rolls."""
    _since, until = local_day_bounds_utc(tz)
    return datetime.fromisoformat(until)


def already_sent_today(settings: Settings, conn) -> bool:
    since, until = local_day_bounds_utc(settings.timezone)
    return digest_sent_today(conn, datetime.fromisoformat(since), datetime.fromisoformat(until))


def digest_due(settings: Settings, conn, now: datetime) -> bool:
    """Past the configured time today, and nothing sent since local midnight.

    The second half has to be a database check: run_digest has no idempotency
    guard of its own, so a restart at 08:05 would otherwise re-send. Reading
    email_log also means the guard survives the daemon being restarted, killed,
    or run on a different day."""
    local_now = now.astimezone(ZoneInfo(settings.timezone))
    hour, minute = _digest_time(settings)
    if (local_now.hour, local_now.minute) < (hour, minute):
        return False

    return not already_sent_today(settings, conn)


# --- child processes -------------------------------------------------------


# Children log through their own console handler, which writes
# `<ts> | LEVEL | ...`. Mirroring that level here keeps daemon.log readable
# instead of flattening a whole run to one level.
_CHILD_LEVEL = re.compile(r"^\S+ \S+ \| (DEBUG|INFO|WARNING|ERROR|CRITICAL)\b")


def _pump(stream, script_name: str, last_line: list[str]) -> None:
    """Log a child's output line by line, as it arrives.

    This used to be `proc.communicate()`, which buffers everything until the
    process exits — so a 40-minute poll showed nothing at all until it
    finished (or until Ctrl-C killed it and dumped 40 minutes of log in one
    burst). A long-running poll is exactly when you most want to watch."""
    try:
        for raw in iter(stream.readline, ""):
            line = raw.rstrip("\n")
            if not line:
                continue
            last_line[0] = line
            match = _CHILD_LEVEL.match(line)
            level = getattr(logging, match.group(1)) if match else logging.INFO
            logger.log(level, "[%s] %s", script_name, line)
    finally:
        stream.close()


def run_child(script: Path, timeout_minutes: float, conn) -> tuple[int, str]:
    """Run one of the CLI entrypoints and return (exit_code, summary line).

    sys.executable is already the venv interpreter when the daemon is launched
    with `uv run`, so this is exactly `uv run python scripts/run_poll.py`
    without re-resolving the lockfile every thirty minutes."""
    global _child

    command = [sys.executable, str(script)]
    logger.info("running %s (timeout %.0f min)", script.name, timeout_minutes)
    started = datetime.now(UTC)

    # The child's logging goes to stderr, which Python line-buffers; its final
    # summary goes to stdout, which is block-buffered on a pipe. Unbuffering
    # makes both stream immediately.
    env = {**os.environ, "PYTHONUNBUFFERED": "1"}

    proc = subprocess.Popen(
        command,
        cwd=_REPO_ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
        env=env,
    )
    with _child_lock:
        _child = proc

    last_stdout: list[str] = [""]
    pumps = [
        threading.Thread(
            target=_pump, args=(proc.stdout, script.name, last_stdout), daemon=True
        ),
        threading.Thread(target=_pump, args=(proc.stderr, script.name, [""]), daemon=True),
    ]
    for pump in pumps:
        pump.start()

    timed_out = False
    try:
        proc.wait(timeout=timeout_minutes * 60)
    except subprocess.TimeoutExpired:
        timed_out = True
        proc.kill()
        proc.wait()
    finally:
        for pump in pumps:
            pump.join(timeout=5)
        with _child_lock:
            _child = None

    elapsed = (datetime.now(UTC) - started).total_seconds()

    if timed_out:
        message = f"{script.name} exceeded its {timeout_minutes:.0f} minute timeout and was killed"
        log_error(
            conn,
            stage="daemon",
            message=message,
            source_file=_SOURCE_FILE,
            function_name="run_child",
            job_ref=script.name,
        )
        return exit_codes.FAILURE, message

    logger.info("%s exited %d after %.0fs", script.name, proc.returncode, elapsed)
    return proc.returncode, last_stdout[0]


# --- the loop --------------------------------------------------------------


def _handle_poll_exit(
    code: int,
    summary: str,
    settings: Settings,
    state: DaemonState,
    *,
    shutting_down: bool = False,
) -> None:
    now = datetime.now(UTC)
    if shutting_down:
        # We killed it ourselves on Ctrl-C. Not a failure, and nothing
        # already committed is lost — upsert_job commits per job.
        logger.info("run_poll stopped by shutdown (exit %d)", code)
        _update(last_poll_finished_at=now, last_poll_exit_code=code)
        return
    failures = 0 if code == exit_codes.OK else state.consecutive_poll_failures + 1

    fields: dict[str, object] = {
        "last_poll_finished_at": now,
        "last_poll_exit_code": code,
        "last_poll_summary": summary or None,
        "consecutive_poll_failures": failures,
    }

    if code == exit_codes.BUDGET_EXCEEDED:
        paused_until = next_local_midnight(settings.timezone)
        logger.critical(
            "daily spend ceiling hit — suspending polls until %s", paused_until.isoformat()
        )
        fields["budget_paused_until"] = paused_until
    elif code == exit_codes.RESUMES_NOT_READY:
        # scope.md §10's hard gate: the run aborts, but the daemon keeps
        # ticking so dropping a fixed resume into config/resumes/ is picked up
        # on the next spawn without a restart.
        logger.critical("resume set is not ready — the run aborted; daemon will retry next cycle")
    elif code == exit_codes.CONFIG_ERROR:
        logger.critical("configuration is invalid — fix .env; daemon will retry next cycle")
    elif code != exit_codes.OK:
        logger.error("run_poll exited %d (see the errors table / logs)", code)

    _update(**fields)


def _tick(settings: Settings, stop: threading.Event) -> None:
    """One pass of the loop. Never raises — the daemon must outlive any single
    failure — but deliberately catches Exception, not BaseException, so
    KeyboardInterrupt and SystemExit still get through. (Same discipline as
    pipeline.run_poll's explicit `except BudgetExceeded: raise` carve-out:
    a blanket handler here would silently eat the shutdown signal.)"""
    now = datetime.now(UTC)

    with connection(settings.db_path) as conn:
        state = get_state()
        assert state is not None

        if state.budget_paused_until is not None and now >= state.budget_paused_until:
            logger.info("local day rolled over — resuming polls")
            state = _update(budget_paused_until=None)

        paused = state.budget_paused_until is not None

        if now >= state.next_poll_at and not paused:
            _update(activity="checking_upstream", last_upstream_check_at=now)
            should_poll = False
            try:
                should_poll = upstream_changed(settings, conn)
                if should_poll:
                    _update(last_upstream_change_at=now)
                elif forced_poll_due(get_state(), settings, now):
                    logger.info(
                        "no upstream change, but %dh since the last poll — polling anyway",
                        settings.force_poll_hours,
                    )
                    should_poll = True
            except httpx.HTTPError as exc:
                # Unknown, not unchanged. Skip this cycle and retry on the next
                # interval rather than guessing in either direction.
                log_error(
                    conn,
                    stage="daemon",
                    exc=exc,
                    source_file=_SOURCE_FILE,
                    function_name="_tick",
                    job_ref="manifest",
                )

            if should_poll and not stop.is_set():
                _update(activity="polling", last_poll_started_at=datetime.now(UTC))
                code, summary = run_child(_POLL_SCRIPT, settings.poll_timeout_minutes, conn)
                _handle_poll_exit(
                    code, summary, settings, get_state(), shutting_down=stop.is_set()
                )

            _update(
                next_poll_at=datetime.now(UTC)
                + timedelta(minutes=settings.poll_interval_minutes)
            )

        if stop.is_set():
            return

        now = datetime.now(UTC)
        if digest_due(settings, conn, now):
            _update(activity="digesting")
            code, _summary = run_child(_DIGEST_SCRIPT, settings.digest_timeout_minutes, conn)
            if code != exit_codes.OK:
                logger.error("run_digest exited %d — see email_log and the errors table", code)

        _update(
            activity="idle",
            digest_sent_today=already_sent_today(settings, conn),
            next_digest_at=next_digest_at(settings, datetime.now(UTC)),
        )


def run_daemon(settings: Settings) -> int:
    """Run until stopped. Returns a process exit code."""
    lock = acquire_singleton_lock(settings.data_dir)
    try:
        sweep_partial_downloads(settings.data_dir)

        now = datetime.now(UTC)
        _set_state(
            DaemonState(
                started_at=now,
                next_poll_at=now,  # always poll once on startup
                next_digest_at=next_digest_at(settings, now),
            )
        )

        with connection(settings.db_path) as conn:
            init_schema(conn)
            _update(upstream_etag=get_daemon_state(conn, _MANIFEST_ETAG_KEY))

        stop = threading.Event()
        install_signal_handlers(stop)

        dashboard = None
        if settings.dashboard_enabled:
            from coldstart.web.app import serve_in_thread

            dashboard = serve_in_thread(settings)

        logger.info(
            "daemon started (pid %d) — polling every %d min, digest at %s %s",
            os.getpid(),
            settings.poll_interval_minutes,
            settings.digest_time_pdt,
            settings.timezone,
        )

        try:
            while not stop.is_set():
                try:
                    _tick(settings, stop)
                except Exception as exc:  # noqa: BLE001 - see _tick's docstring
                    logger.exception("unhandled error in daemon tick: %s", exc)
                    try:
                        with connection(settings.db_path) as conn:
                            log_error(
                                conn,
                                stage="daemon",
                                exc=exc,
                                source_file=_SOURCE_FILE,
                                function_name="run_daemon",
                            )
                    except Exception:  # noqa: BLE001
                        logger.exception("could not record the daemon error to the errors table")
                stop.wait(_TICK_SECONDS)
        finally:
            if dashboard is not None:
                dashboard.stop()

        logger.info("daemon stopped cleanly")
        return exit_codes.OK
    finally:
        lock.close()
