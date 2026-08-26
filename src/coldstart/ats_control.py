"""Module 29 — the operator decides what runs next.

Until now the poll worked through whatever `changed_slices` handed it, in
manifest order, and there was no way to influence that. The order matters
more than it looks: `workday` is 839k rows and legitimately takes hours, so
starting it means every other source waits behind it. If what you actually
wanted today was `ashby`, your only options were to wait or to kill the
daemon.

Two independent controls, kept separate because they answer different
questions:

- **priority** — an ordered "do these first" list. This is a *preference*,
  not a filter: once a prioritised source is done it leaves the list and
  everything else carries on in its normal order.
- **held** — "do not process this at all until I say otherwise". A filter,
  not an ordering. Distinct from `config/excluded_ats.json`, which is a
  permanent design decision about which sources this project cares about;
  this is a switch the operator flips from the page and flips back.

**Why `daemon_state` and not the daemon's memory.** `run_poll` is a
subprocess (see daemon.py), so the only thing it and the web layer share is
the database. `daemon_state` already exists for exactly this — small,
non-business, harmless to lose — and it is what `progress.py` uses in the
other direction.

**Preemption is deliberately cheap, which is what makes it safe to offer.**
Yielding a half-processed slice loses the parquet read and the filter pass —
minutes of CPU — and nothing else. The download is already on disk and
sha256-verified (`fetcher.download_slice` reuses it), and every job scored
before the yield is already committed, so `dedupe` skips it on the re-run.
No LLM spend is ever repeated. A slice that yields does **not** get its
`slice_state` written, so it stays outstanding and comes back round.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Collection, Sequence
from datetime import UTC, datetime

from pydantic import BaseModel, Field

from coldstart.db import get_daemon_state, set_daemon_state
from coldstart.logging_setup import get_logger

logger = get_logger(__name__)

CONTROL_KEY = "ats_control"

# A slice that yields goes back in the queue, which is a loop if something
# keeps sending it away. Nothing in `reason_to_yield` can actually do that —
# a held source is filtered out of the ordering entirely, and a source that
# is itself the top priority never yields — but this is a runaway guard, not
# a target, in the same spirit as FORCE_POLL_HOURS.
def max_yields_for(slice_count: int) -> int:
    return max(4, slice_count * 2)


class AtsControl(BaseModel):
    """The operator's standing instructions about source order."""

    # Ordered. Index 0 runs next.
    priority: list[str] = Field(default_factory=list)
    # Unordered — this is a filter, not a queue.
    held: list[str] = Field(default_factory=list)
    updated_at: datetime | None = None

    def is_held(self, ats_type: str) -> bool:
        return ats_type in self.held

    def rank(self, ats_type: str) -> int:
        """Sort key. Everything unprioritised ties, so a stable sort leaves
        it in the manifest's own order."""
        try:
            return self.priority.index(ats_type)
        except ValueError:
            return len(self.priority)


def read_control(conn: sqlite3.Connection) -> AtsControl:
    """The current instructions, or an empty set of them.

    Never raises: an unreadable control row must mean "no preferences", not a
    crashed poll. The pipeline reads this between every job."""
    raw = get_daemon_state(conn, CONTROL_KEY)
    if not raw:
        return AtsControl()
    try:
        return AtsControl.model_validate(json.loads(raw))
    except Exception:
        logger.warning("stored ats control is not readable — ignoring it")
        return AtsControl()


def write_control(conn: sqlite3.Connection, control: AtsControl) -> AtsControl:
    control.updated_at = datetime.now(UTC)
    set_daemon_state(conn, CONTROL_KEY, control.model_dump_json())
    return control


# --- the four operator actions ---------------------------------------------


def run_next(conn: sqlite3.Connection, ats_type: str) -> AtsControl:
    """Put this source at the front of the queue.

    Also releases it if it was held — "run this next" and "never run this"
    are contradictory instructions, and the newer one is the one meant."""
    control = read_control(conn)
    control.priority = [ats_type, *(a for a in control.priority if a != ats_type)]
    control.held = [a for a in control.held if a != ats_type]
    logger.info("ats control: %s moved to the front of the queue", ats_type)
    return write_control(conn, control)


def hold(conn: sqlite3.Connection, ats_type: str) -> AtsControl:
    """Skip this source until it is released. Drops any priority it had."""
    control = read_control(conn)
    if ats_type not in control.held:
        control.held.append(ats_type)
    control.priority = [a for a in control.priority if a != ats_type]
    logger.info("ats control: %s held", ats_type)
    return write_control(conn, control)


def release(conn: sqlite3.Connection, ats_type: str) -> AtsControl:
    control = read_control(conn)
    control.held = [a for a in control.held if a != ats_type]
    logger.info("ats control: %s released", ats_type)
    return write_control(conn, control)


def clear_priority(conn: sqlite3.Connection, ats_type: str) -> AtsControl:
    control = read_control(conn)
    control.priority = [a for a in control.priority if a != ats_type]
    logger.info("ats control: %s back to its normal place in the queue", ats_type)
    return write_control(conn, control)


def reset(conn: sqlite3.Connection) -> AtsControl:
    """Back to "whatever the manifest says, in its own order"."""
    logger.info("ats control: reset")
    return write_control(conn, AtsControl())


# --- what the pipeline asks -------------------------------------------------


def order_slices[S](
    slices: Sequence[S], control: AtsControl, *, key=lambda s: s.ats_type
) -> tuple[list[S], list[S]]:
    """Split into (runnable in the order to run them, held back).

    The sort is stable and everything unprioritised shares one rank, so a
    source with no priority set keeps the position the manifest gave it —
    setting a priority reorders exactly what you asked for and nothing else."""
    held = [s for s in slices if control.is_held(key(s))]
    runnable = [s for s in slices if not control.is_held(key(s))]
    runnable.sort(key=lambda s: control.rank(key(s)))
    return runnable, held


def reason_to_yield(
    control: AtsControl, current: str, pending: Collection[str]
) -> str | None:
    """Should the slice being processed right now stop and give way?

    Returns a short reason for the log and the dashboard, or None to carry
    on. `pending` is the set of sources this run still has left — a priority
    pointing at something already done, or at a source not in this run at
    all, is not a reason to abandon useful work.

    Walking `priority` in order is what makes this correct: if `current` is
    itself the highest-priority pending source, it is exactly what the
    operator asked for and must never yield to a lower-priority one."""
    if control.is_held(current):
        return "held"
    for ats_type in control.priority:
        if ats_type == current:
            return None
        if ats_type in pending:
            return f"priority:{ats_type}"
    return None
