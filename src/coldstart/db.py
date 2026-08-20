from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from coldstart.models import JobRecord, SliceState

_SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
  global_id        TEXT PRIMARY KEY,
  requisition_id   TEXT,
  company          TEXT NOT NULL,
  title            TEXT NOT NULL,
  location         TEXT,
  apply_url        TEXT,
  ats_type         TEXT NOT NULL,
  posted_at        TEXT,
  resume_used      TEXT,
  score            INTEGER,
  score_band       TEXT,
  eligible         INTEGER,
  matched_skills   TEXT,
  missing_skills   TEXT,
  reasoning        TEXT,
  status           TEXT NOT NULL,
  location_flag    TEXT NOT NULL,
  eligibility_flag TEXT NOT NULL,
  provider_used    TEXT,
  first_seen_at    TEXT NOT NULL,
  scored_at        TEXT
);

CREATE TABLE IF NOT EXISTS errors (
  id            INTEGER PRIMARY KEY,
  ts            TEXT NOT NULL,
  stage         TEXT NOT NULL,
  source_file   TEXT NOT NULL,
  function_name TEXT NOT NULL,
  provider      TEXT,
  job_ref       TEXT,
  error_type    TEXT,
  error_message TEXT,
  retry_count   INTEGER DEFAULT 0,
  resolved      INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS email_log (
  id        INTEGER PRIMARY KEY,
  sent_at   TEXT NOT NULL,
  job_count INTEGER NOT NULL,
  status    TEXT NOT NULL,
  error     TEXT
);

CREATE TABLE IF NOT EXISTS slice_state (
  ats_type          TEXT PRIMARY KEY,
  last_sha256       TEXT,
  last_processed_at TEXT,
  row_count         INTEGER
);

CREATE TABLE IF NOT EXISTS spend_log (
  id            INTEGER PRIMARY KEY,
  ts            TEXT NOT NULL,
  provider      TEXT NOT NULL,
  model         TEXT NOT NULL,
  input_tokens  INTEGER,
  cached_tokens INTEGER,
  output_tokens INTEGER,
  est_cost_usd  REAL,
  job_ref       TEXT
);

-- One row per run_poll invocation (Module 19). fetched/filtered rows are
-- deliberately never persisted to `jobs` (title/location-rejected volume
-- would be enormous — see DEVELOPMENT_PLAN.md Module 19), so this is the
-- only durable record of those funnel counts; run_digest sums today's rows
-- to build the digest footer, since a single run_poll's in-memory result
-- doesn't survive past that process.
CREATE TABLE IF NOT EXISTS run_log (
  id             INTEGER PRIMARY KEY,
  run_id         TEXT NOT NULL,
  started_at     TEXT NOT NULL,
  finished_at    TEXT NOT NULL,
  fetched_count  INTEGER NOT NULL DEFAULT 0,
  filtered_count INTEGER NOT NULL DEFAULT 0,
  scored_count   INTEGER NOT NULL DEFAULT 0,
  failed_count   INTEGER NOT NULL DEFAULT 0
);

-- Your decisions about a job, kept OUT of the `jobs` table on purpose.
-- `jobs` is pipeline output and gets rewritten by upsert_job's ON CONFLICT
-- clause every time a posting is re-scored, which would silently wipe an
-- "applied" mark. A separate table can't be clobbered that way, and it also
-- survives a row being rebuilt from scratch.
CREATE TABLE IF NOT EXISTS job_state (
  global_id  TEXT PRIMARY KEY,
  state      TEXT NOT NULL,
  updated_at TEXT NOT NULL,
  note       TEXT
);

CREATE INDEX IF NOT EXISTS idx_job_state_state ON job_state(state);

-- Small key/value store for the daemon's own cross-restart state (Module 20).
-- Only the manifest ETag lives here today: it lets the 30-minute upstream
-- check survive a restart as a conditional GET instead of re-downloading the
-- manifest body. Deliberately not a typed table — nothing here is a business
-- record, and a restart losing any of it is harmless by construction.
CREATE TABLE IF NOT EXISTS daemon_state (
  key        TEXT PRIMARY KEY,
  value      TEXT,
  updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_jobs_requisition_id ON jobs(requisition_id);
CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_score_band ON jobs(score_band);
CREATE INDEX IF NOT EXISTS idx_jobs_scored_at ON jobs(scored_at);
CREATE INDEX IF NOT EXISTS idx_jobs_first_seen_at ON jobs(first_seen_at);
CREATE INDEX IF NOT EXISTS idx_errors_resolved ON errors(resolved);
CREATE INDEX IF NOT EXISTS idx_errors_stage ON errors(stage);
CREATE INDEX IF NOT EXISTS idx_spend_log_ts ON spend_log(ts);
CREATE INDEX IF NOT EXISTS idx_run_log_started_at ON run_log(started_at);
"""

_JOB_UPSERT = """
INSERT INTO jobs (
    global_id, requisition_id, company, title, location, apply_url,
    ats_type, posted_at, resume_used, score, score_band, eligible,
    matched_skills, missing_skills, reasoning, status, location_flag,
    eligibility_flag, provider_used, first_seen_at, scored_at
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
ON CONFLICT(global_id) DO UPDATE SET
    requisition_id=excluded.requisition_id,
    company=excluded.company,
    title=excluded.title,
    location=excluded.location,
    apply_url=excluded.apply_url,
    ats_type=excluded.ats_type,
    posted_at=excluded.posted_at,
    resume_used=excluded.resume_used,
    score=excluded.score,
    score_band=excluded.score_band,
    eligible=excluded.eligible,
    matched_skills=excluded.matched_skills,
    missing_skills=excluded.missing_skills,
    reasoning=excluded.reasoning,
    status=excluded.status,
    location_flag=excluded.location_flag,
    eligibility_flag=excluded.eligibility_flag,
    provider_used=excluded.provider_used,
    scored_at=excluded.scored_at
"""


@contextmanager
def connection(db_path: Path) -> Iterator[sqlite3.Connection]:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def readonly_connection(db_path: Path) -> Iterator[sqlite3.Connection]:
    """A read-only connection, for the dashboard (Module 21).

    `mode=ro` makes it structurally impossible for a web request to lock or
    write the database while run_poll is committing to it. WAL (set by
    connection() above, and persistent on the file) is what lets this read
    concurrently with those writes; busy_timeout covers the brief exclusive
    lock a WAL checkpoint takes.

    Open one of these per request and let it close — sqlite3 connections
    default to check_same_thread=True and must not be shared across the
    server's threadpool."""
    db_path = Path(db_path)
    if not db_path.exists():
        # mode=ro refuses to create the file, so it would raise an opaque
        # OperationalError. The dashboard renders an empty state instead.
        raise FileNotFoundError(db_path)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=5000")
    try:
        yield conn
    finally:
        conn.close()


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    conn.commit()


def load_seen_keys(
    conn: sqlite3.Connection,
) -> tuple[set[str], set[tuple[str, str, str]]]:
    global_ids = {row[0] for row in conn.execute("SELECT global_id FROM jobs")}
    # (company, requisition_id, location) — bare requisition_id collides across
    # unrelated companies/postings on real data; see DEVELOPMENT_PLAN.md Module 10.
    req_keys = {
        (row["company"], row["requisition_id"], row["location"])
        for row in conn.execute(
            "SELECT company, requisition_id, location FROM jobs WHERE requisition_id IS NOT NULL"
        )
    }
    return global_ids, req_keys


def upsert_job(conn: sqlite3.Connection, job: JobRecord) -> None:
    conn.execute(_JOB_UPSERT, _job_to_row(job))
    conn.commit()


def get_digest_jobs(conn: sqlite3.Connection, since: datetime) -> list[JobRecord]:
    rows = conn.execute(
        """
        SELECT * FROM jobs
        WHERE datetime(COALESCE(scored_at, first_seen_at)) >= datetime(?)
        ORDER BY score IS NULL, score DESC
        """,
        (since.isoformat(),),
    ).fetchall()
    return [_row_to_job(row) for row in rows]


def get_slice_state(conn: sqlite3.Connection, ats_type: str) -> SliceState | None:
    row = conn.execute(
        "SELECT * FROM slice_state WHERE ats_type = ?", (ats_type,)
    ).fetchone()
    if row is None:
        return None
    return SliceState(
        ats_type=row["ats_type"],
        last_sha256=row["last_sha256"],
        last_processed_at=row["last_processed_at"],
        row_count=row["row_count"],
    )


def set_slice_state(conn: sqlite3.Connection, state: SliceState) -> None:
    conn.execute(
        """
        INSERT INTO slice_state (ats_type, last_sha256, last_processed_at, row_count)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(ats_type) DO UPDATE SET
            last_sha256=excluded.last_sha256,
            last_processed_at=excluded.last_processed_at,
            row_count=excluded.row_count
        """,
        (
            state.ats_type,
            state.last_sha256,
            state.last_processed_at.isoformat() if state.last_processed_at else None,
            state.row_count,
        ),
    )
    conn.commit()


def log_email(
    conn: sqlite3.Connection,
    sent_at: datetime,
    job_count: int,
    status: str,
    error: str | None = None,
) -> None:
    conn.execute(
        "INSERT INTO email_log (sent_at, job_count, status, error) VALUES (?, ?, ?, ?)",
        (sent_at.isoformat(), job_count, status, error),
    )
    conn.commit()


def log_run(
    conn: sqlite3.Connection,
    run_id: str,
    started_at: datetime,
    finished_at: datetime,
    fetched_count: int,
    filtered_count: int,
    scored_count: int,
    failed_count: int,
) -> None:
    conn.execute(
        """
        INSERT INTO run_log (
            run_id, started_at, finished_at, fetched_count, filtered_count,
            scored_count, failed_count
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            started_at.isoformat(),
            finished_at.isoformat(),
            fetched_count,
            filtered_count,
            scored_count,
            failed_count,
        ),
    )
    conn.commit()


def run_totals_since(conn: sqlite3.Connection, since: datetime) -> dict[str, int]:
    row = conn.execute(
        """
        SELECT
            COALESCE(SUM(fetched_count), 0)  AS fetched,
            COALESCE(SUM(filtered_count), 0) AS filtered,
            COALESCE(SUM(scored_count), 0)   AS scored,
            COALESCE(SUM(failed_count), 0)   AS failed
        FROM run_log
        WHERE started_at >= ?
        """,
        (since.isoformat(),),
    ).fetchone()
    return {
        "fetched": row["fetched"],
        "filtered": row["filtered"],
        "scored": row["scored"],
        "failed": row["failed"],
    }


def providers_used_since(conn: sqlite3.Connection, since: datetime) -> list[str]:
    rows = conn.execute(
        "SELECT DISTINCT provider FROM spend_log WHERE ts >= ? ORDER BY provider",
        (since.isoformat(),),
    ).fetchall()
    return [row["provider"] for row in rows]


def count_unresolved_errors(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) FROM errors WHERE resolved = 0").fetchone()
    return row[0]


# The only states the dashboard may set. Kept deliberately small — an
# unrecognised value from a request must never reach the database.
JOB_STATES = frozenset({"applied"})


def set_job_state(
    conn: sqlite3.Connection, global_id: str, state: str, note: str | None = None
) -> None:
    if state not in JOB_STATES:
        raise ValueError(f"unknown job state {state!r}; expected one of {sorted(JOB_STATES)}")
    conn.execute(
        """
        INSERT INTO job_state (global_id, state, updated_at, note)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(global_id) DO UPDATE SET
            state=excluded.state,
            updated_at=excluded.updated_at,
            note=excluded.note
        """,
        (global_id, state, datetime.now(UTC).isoformat(), note),
    )
    conn.commit()


def clear_job_state(conn: sqlite3.Connection, global_id: str) -> None:
    conn.execute("DELETE FROM job_state WHERE global_id = ?", (global_id,))
    conn.commit()


def job_states(conn: sqlite3.Connection) -> dict[str, str]:
    """Every marked job, as {global_id: state}.

    One query for the whole set rather than a join per listing: at these
    volumes it's a few hundred rows at most, and it keeps the jobs query
    itself unchanged."""
    return {row["global_id"]: row["state"] for row in conn.execute(
        "SELECT global_id, state FROM job_state"
    )}


def get_daemon_state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM daemon_state WHERE key = ?", (key,)).fetchone()
    return None if row is None else row["value"]


def set_daemon_state(conn: sqlite3.Connection, key: str, value: str | None) -> None:
    conn.execute(
        """
        INSERT INTO daemon_state (key, value, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(key) DO UPDATE SET
            value=excluded.value,
            updated_at=excluded.updated_at
        """,
        (key, value, datetime.now(UTC).isoformat()),
    )
    conn.commit()


def last_digest_sent_at(conn: sqlite3.Connection) -> datetime | None:
    """When the last digest actually went out, or None if none ever has.

    Only successful sends count. If a send failed, the window must NOT
    advance — the next successful digest has to cover that period too, or the
    jobs in it are never reported to anyone.

    This is what makes digest coverage continuous. The window used to be
    "since local midnight", which meant an 8am digest reported only the
    overnight hours and everything found between 8am and midnight was never
    emailed at all — a 16-hour blind spot, every day."""
    row = conn.execute(
        "SELECT MAX(sent_at) AS last FROM email_log WHERE status = 'sent'"
    ).fetchone()
    if row is None or row["last"] is None:
        return None
    return datetime.fromisoformat(row["last"])


def digest_sent_today(conn: sqlite3.Connection, since: datetime, until: datetime) -> bool:
    """Has a digest already gone out in the current local day?

    run_digest has no idempotency guard of its own — email_log was write-only
    until now (nothing ever read it), so two calls in one day send two
    identical emails. The daemon checks this before firing, which also means a
    restart at 08:05 doesn't re-send. Bounds come from
    budget.local_day_bounds_utc, same as run_digest's own "today" window."""
    row = conn.execute(
        """
        SELECT 1 FROM email_log
        WHERE status = 'sent' AND sent_at >= ? AND sent_at < ?
        LIMIT 1
        """,
        (since.isoformat(), until.isoformat()),
    ).fetchone()
    return row is not None


def _job_to_row(job: JobRecord) -> tuple:
    return (
        job.global_id,
        job.requisition_id,
        job.company,
        job.title,
        job.location,
        job.apply_url,
        job.ats_type,
        job.posted_at.isoformat() if job.posted_at else None,
        job.resume_used.value if job.resume_used else None,
        job.score,
        job.score_band.value if job.score_band else None,
        None if job.eligible is None else int(job.eligible),
        json.dumps(job.matched_skills),
        json.dumps(job.missing_skills),
        job.reasoning,
        job.status.value,
        job.location_flag.value,
        job.eligibility_flag.value,
        job.provider_used,
        job.first_seen_at.isoformat(),
        job.scored_at.isoformat() if job.scored_at else None,
    )


def _row_to_job(row: sqlite3.Row) -> JobRecord:
    return JobRecord(
        global_id=row["global_id"],
        requisition_id=row["requisition_id"],
        company=row["company"],
        title=row["title"],
        location=row["location"],
        apply_url=row["apply_url"],
        ats_type=row["ats_type"],
        posted_at=row["posted_at"],
        resume_used=row["resume_used"],
        score=row["score"],
        score_band=row["score_band"],
        eligible=None if row["eligible"] is None else bool(row["eligible"]),
        matched_skills=json.loads(row["matched_skills"]) if row["matched_skills"] else [],
        missing_skills=json.loads(row["missing_skills"]) if row["missing_skills"] else [],
        reasoning=row["reasoning"],
        status=row["status"],
        location_flag=row["location_flag"],
        eligibility_flag=row["eligibility_flag"],
        provider_used=row["provider_used"],
        first_seen_at=row["first_seen_at"],
        scored_at=row["scored_at"],
    )
