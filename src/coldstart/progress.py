"""Module 28 — what the poll is doing *right now*.

`DaemonState.activity` says `"polling"` and nothing more. A poll legitimately
runs for hours (36 slices; the workday one alone is 839k rows), so on its own
that flag cannot tell "working through slice 14 of 22" apart from "wedged
three hours ago". The operator's real question — *what is it doing this
second* — had no answer anywhere.

run_poll is a **subprocess**, not a function call (daemon.py's module
docstring explains why: a ~10 GB RSS peak has to go back to the OS between
cycles). So it cannot touch the daemon's in-memory `DaemonState`. The
database is the only channel the two processes share, and `daemon_state`
already exists for exactly this role — small, non-business, harmless to lose.

**A stale heartbeat must never read as "running".** A SIGKILL — which is
precisely how a poll that overran `POLL_TIMEOUT_MINUTES` dies, and that has
really happened here — leaves the last row behind untouched. `is_running`
therefore demands both a recent `updated_at` *and* a live pid. The asymmetry
is deliberate: showing "idle" a few seconds after a poll starts is cosmetic,
while showing "running" forever after a kill would hide a stalled pipeline,
which is the whole failure this exists to make visible.
"""

from __future__ import annotations

import json
import os
import sqlite3
from datetime import UTC, datetime

from pydantic import BaseModel

from coldstart.db import get_daemon_state, set_daemon_state
from coldstart.logging_setup import get_logger

logger = get_logger(__name__)

PROGRESS_KEY = "poll_progress"

# The scoring loop heartbeats at most this often. One LLM call takes seconds,
# so per-job writes would be pure noise; five seconds is well under the
# staleness window below and costs one tiny UPSERT.
HEARTBEAT_SECONDS = 5.0

# Older than this with no update and we stop calling it live. Generous on
# purpose: a single slice download can stall for a while on a slow link
# without the run being dead, and the pid check below is the sharper test.
STALE_AFTER_SECONDS = 180.0


class PollProgress(BaseModel):
    """One heartbeat from a running poll. Everything is a plain scalar — this
    is serialised to JSON and read back by a process that has no access to the
    poll's objects."""

    run_id: str
    pid: int
    started_at: datetime
    updated_at: datetime

    # Where the run is in its slice list. `slice_index` is 1-based for display
    # ("slice 3 of 12"), and is 0 before the first slice starts.
    slice_index: int = 0
    slice_total: int = 0
    ats_type: str | None = None

    # What the current slice is doing. Named for what an operator would say,
    # not for the function that is on the stack.
    phase: str = "starting"

    # Progress inside the current slice.
    slice_rows: int = 0          # rows in the snapshot, from the manifest
    slice_fetched: int = 0       # rows actually read off the parquet
    slice_candidates: int = 0    # rows that survived every filter — the scoring queue
    slice_processed: int = 0     # how far through that queue we are

    # Running totals for the whole run, so a killed poll still leaves an
    # honest record of what it managed to do.
    scored: int = 0
    failed: int = 0
    excluded: int = 0
    location_excluded: int = 0
    stack_excluded: int = 0
    delisted: int = 0

    finished_at: datetime | None = None

    @property
    def age_seconds(self) -> float:
        return (datetime.now(UTC) - self.updated_at).total_seconds()

    @property
    def pid_alive(self) -> bool:
        """Signal 0 checks for the process without touching it. A poll's pid
        belongs to the same user as the dashboard, so PermissionError would be
        surprising — but it still means "exists", which is the question."""
        try:
            os.kill(self.pid, 0)
        except ProcessLookupError:
            return False
        except (PermissionError, OSError):
            return True
        return True

    @property
    def is_running(self) -> bool:
        if self.finished_at is not None:
            return False
        return self.age_seconds <= STALE_AFTER_SECONDS and self.pid_alive


def write_progress(conn: sqlite3.Connection, progress: PollProgress) -> None:
    """Persist a heartbeat. Never raises.

    A poll must not die because its own progress reporting hit a locked
    database. This is a diagnostic, and a missed beat costs nothing beyond a
    briefly stale page."""
    try:
        set_daemon_state(conn, PROGRESS_KEY, progress.model_dump_json())
    except Exception:  # pragma: no cover - defensive, sqlite-state dependent
        logger.debug("could not write poll progress", exc_info=True)


def read_progress(conn: sqlite3.Connection) -> PollProgress | None:
    """The last heartbeat, or None if there has never been one.

    Returns None rather than raising on a malformed value: an unreadable
    diagnostic is a missing diagnostic, not a broken dashboard."""
    raw = get_daemon_state(conn, PROGRESS_KEY)
    if not raw:
        return None
    try:
        return PollProgress.model_validate(json.loads(raw))
    except Exception:
        logger.warning("stored poll progress is not readable — ignoring it")
        return None
