"""Push strong matches to hemline (Module 38, scope.md §8.4).

Every job scored at or above HEMLINE_MIN_SCORE is POSTed to HEMLINE_URL the
moment it is scored — one request per job, no batching. The job is written to
`hemline_events` first and sent second, so a send that fails (hemline down,
wifi off, bad token) leaves the job owed rather than lost, and the start of
every poll retries whatever is still owed.

Two guarantees, both enforced here and in db.py rather than left to callers:

- Only jobs scored after this existed are ever sent. A row is created only by
  `job_scored`, at score time — never by a sweep over `jobs` — so the jobs
  already in the database can't leak out.
- A job is sent at most once. It gets one row, ever (INSERT OR IGNORE), and
  only a row still marked 'pending' is ever sent.

Nothing here may raise into the pipeline. Scoring is the paid-for work; a
hemline problem must never stop a poll or throw a score away.
"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid

import httpx
from pydantic import BaseModel

from coldstart.db import (
    get_hemline_event,
    mark_hemline_sent,
    pending_hemline_events,
    queue_hemline_event,
    record_hemline_failure,
)
from coldstart.errors import log_error
from coldstart.logging_setup import get_logger
from coldstart.models import JobRecord, JobStatus, ResumeId
from coldstart.settings import Settings

logger = get_logger(__name__)

# hemline's names for the four resumes routing.py chooses between.
PROFILE_IDS: dict[ResumeId, str] = {
    ResumeId.A: "software-engineer",
    ResumeId.B: "ai-engineer",
    ResumeId.C: "forward-deployed-engineer-swe",
    ResumeId.D: "forward-deployed-engineer-ai",
}

_TIMEOUT_SECONDS = 10.0
_INLINE_RETRY_DELAY_SECONDS = 2.0
# Delivery rounds, not HTTP requests. After the first, each round is the start
# of a later poll, so this is a day or more of trying before giving up — and
# giving up is written to `errors`, where the dashboard counts it.
_MAX_ATTEMPTS = 10


class _Failure(BaseModel):
    reason: str
    # Worth trying again soon: hemline unreachable, timing out, or erroring on
    # its side. A 4xx means the request itself was refused and an immediate
    # retry would get the same answer.
    transient: bool


def is_enabled(settings: Settings) -> bool:
    token = settings.hemline_token
    return token is not None and bool(token.get_secret_value().strip())


def describe(settings: Settings) -> str:
    if not is_enabled(settings):
        return "off (HEMLINE_TOKEN not set)"
    return f"on — jobs scoring {settings.hemline_min_score}+ go to {settings.hemline_url}"


def event_id_for(global_id: str) -> str:
    """Same job, same id, every time. If a request times out after hemline
    actually received it, the retry carries an id hemline already has, so it
    can drop the repeat instead of storing the job twice."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"coldstart:{global_id}"))


def build_payload(record: JobRecord) -> dict | None:
    if record.resume_used is None or not record.apply_url:
        return None
    return {
        "id": event_id_for(record.global_id),
        "job_title": record.title,
        "profile_id": PROFILE_IDS[record.resume_used],
        "company": (record.company or "").strip() or None,
        "job_link": record.apply_url,
    }


def _post(settings: Settings, payload: dict) -> _Failure | None:
    """None on success, otherwise what went wrong."""
    token = settings.hemline_token.get_secret_value().strip()
    try:
        response = httpx.post(
            settings.hemline_url,
            json=payload,
            headers={"Authorization": f"Bearer {token}"},
            timeout=_TIMEOUT_SECONDS,
        )
    except httpx.HTTPError as exc:
        return _Failure(reason=f"{type(exc).__name__}: {exc}", transient=True)

    # 409 is taken to mean "already have this id" — the retry-after-timeout
    # case event_id_for exists for. Either way hemline has the job.
    if response.is_success or response.status_code == 409:
        return None
    status = response.status_code
    return _Failure(
        reason=f"HTTP {status}: {response.text[:200]}",
        transient=status >= 500 or status == 429,
    )


def _deliver(
    conn: sqlite3.Connection, global_id: str, settings: Settings, *, inline_retry: bool
) -> _Failure | None:
    row = get_hemline_event(conn, global_id)
    # The at-most-once guard. Whoever calls this, a job that already went out
    # (or that was given up on) is never sent again.
    if row is None or row["status"] != "pending":
        return None

    payload = json.loads(row["payload"])
    failure = _post(settings, payload)
    if failure is not None and failure.transient and inline_retry:
        time.sleep(_INLINE_RETRY_DELAY_SECONDS)
        failure = _post(settings, payload)

    if failure is None:
        mark_hemline_sent(conn, global_id)
        logger.info(
            "hemline: sent %r at %s as %s",
            payload["job_title"], payload["company"], payload["profile_id"],
        )
        return None

    attempts = row["attempts"] + 1
    give_up = attempts >= _MAX_ATTEMPTS
    record_hemline_failure(conn, global_id, failure.reason, give_up=give_up)
    if give_up:
        log_error(
            conn,
            stage="hemline",
            message=f"gave up after {attempts} attempts: {failure.reason}",
            source_file=__name__,
            function_name="_deliver",
            job_ref=global_id,
            retry_count=attempts,
        )
    else:
        logger.warning(
            "hemline: could not send %s (attempt %d of %d, retried next poll): %s",
            global_id, attempts, _MAX_ATTEMPTS, failure.reason,
        )
    return failure


def job_scored(conn: sqlite3.Connection, record: JobRecord, settings: Settings) -> None:
    """Called by the pipeline straight after a scored job is saved."""
    if (
        not is_enabled(settings)
        or record.status is not JobStatus.SCORED
        or record.score is None
        or record.score < settings.hemline_min_score
    ):
        return
    try:
        payload = build_payload(record)
        if payload is None:
            logger.warning(
                "hemline: %s scored %d but has no link or resume — not sent",
                record.global_id, record.score,
            )
            return
        if not queue_hemline_event(conn, record.global_id, payload["id"], json.dumps(payload)):
            return
        _deliver(conn, record.global_id, settings, inline_retry=True)
    except Exception as exc:
        log_error(
            conn,
            stage="hemline",
            exc=exc,
            source_file=__name__,
            function_name="job_scored",
            job_ref=record.global_id,
        )


def retry_unsent(conn: sqlite3.Connection, settings: Settings) -> int:
    """Send what earlier attempts couldn't; returns how many went out. Runs at
    the start of every poll, including the ones that find nothing new.

    Stops at the first sign hemline is unreachable: the rest stay owed for the
    next poll rather than each waiting out its own timeout first."""
    if not is_enabled(settings):
        return 0
    try:
        pending = pending_hemline_events(conn)
        if not pending:
            return 0
        logger.info("hemline: retrying %d unsent job(s)", len(pending))
        sent = 0
        for tried, global_id in enumerate(pending):
            failure = _deliver(conn, global_id, settings, inline_retry=False)
            if failure is None:
                sent += 1
            elif failure.transient:
                logger.info(
                    "hemline: unreachable, leaving %d job(s) for the next poll",
                    len(pending) - tried,
                )
                break
        return sent
    except Exception as exc:
        log_error(
            conn,
            stage="hemline",
            exc=exc,
            source_file=__name__,
            function_name="retry_unsent",
        )
        return 0
