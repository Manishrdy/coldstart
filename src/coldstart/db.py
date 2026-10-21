from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterable, Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import pandas as pd

from coldstart.filters.freshness import is_fresh, posting_age_days
from coldstart.global_ids import PER_EMPLOYER_ATS_TYPES
from coldstart.models import JobRecord, JobStatus, LivenessCandidate, SliceState

_PENDING = JobStatus.PENDING.value

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
  location_reason  TEXT,
  eligibility_flag TEXT NOT NULL,
  provider_used    TEXT,
  first_seen_at    TEXT NOT NULL,
  scored_at        TEXT,
  delist_reason    TEXT,
  delisted_at      TEXT,
  stack_reason     TEXT,
  sponsorship      TEXT,
  sponsorship_reason TEXT
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

-- Decisions about a whole employer rather than one posting (Module 34). Keyed
-- on filters.company.company_key, so "Stripe" and "stripe" are one row.
-- Only "blocked" ("not this company") exists today; a rejection cooldown and
-- the applications cap are derived from job_state, not stored here.
CREATE TABLE IF NOT EXISTS company_state (
  company_key TEXT PRIMARY KEY,
  company     TEXT NOT NULL,
  state       TEXT NOT NULL,
  updated_at  TEXT NOT NULL
);

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

-- One row per job pushed (or still to be pushed) to hemline (Module 38).
-- A row is written only at the moment a job is scored at or above
-- HEMLINE_MIN_SCORE, never for jobs scored before this existed — the operator
-- asked for new jobs only, and the absence of a row is what guarantees it.
-- `payload` is the exact JSON body, frozen at score time, so a retry sends the
-- same thing the first attempt did even if the `jobs` row changes meanwhile.
-- status: 'pending' (not delivered yet), 'sent', 'failed' (gave up).
CREATE TABLE IF NOT EXISTS hemline_events (
  global_id   TEXT PRIMARY KEY,
  event_id    TEXT NOT NULL,
  payload     TEXT NOT NULL,
  status      TEXT NOT NULL,
  attempts    INTEGER NOT NULL DEFAULT 0,
  last_error  TEXT,
  created_at  TEXT NOT NULL,
  sent_at     TEXT
);

CREATE INDEX IF NOT EXISTS idx_hemline_events_status ON hemline_events(status);
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

# Statuses that carry a paid-for LLM verdict. A pre-LLM filter writes its
# held-back rows *before* dedupe — on purpose, so every row it stopped is
# persisted with its reason — which means it also re-sees postings that were
# scored on an earlier poll and are still in the feed. Without the WHERE clause
# below, adding a filter silently replaced those scored rows with unscored
# ones: Module 32's stack filter did exactly that to 384 already-scored jobs
# (3 of them marked applied), wiping score and reasoning. Found 2026-10-20
# while adding the sponsorship filter, which would have done it again.
#
# The guard lives here, in the write, rather than in each filter's caller —
# a rule every future filter has to remember is not a rule. A scored row
# changing its mind deliberately goes through an explicit UPDATE instead
# (mark_delisted, scripts/recheck_sponsorship.py).
_VERDICT_STATUSES = (JobStatus.SCORED.value, JobStatus.DELISTED.value)
_VERDICT_SQL = ", ".join(f"'{status}'" for status in _VERDICT_STATUSES)

_JOB_UPSERT = f"""
INSERT INTO jobs (
    global_id, requisition_id, company, title, location, apply_url,
    ats_type, posted_at, resume_used, score, score_band, eligible,
    matched_skills, missing_skills, reasoning, status, location_flag,
    location_reason, eligibility_flag, provider_used, first_seen_at, scored_at,
    delist_reason, delisted_at, stack_reason, sponsorship, sponsorship_reason
) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
    location_reason=excluded.location_reason,
    eligibility_flag=excluded.eligibility_flag,
    provider_used=excluded.provider_used,
    scored_at=excluded.scored_at,
    delist_reason=excluded.delist_reason,
    delisted_at=excluded.delisted_at,
    stack_reason=excluded.stack_reason,
    sponsorship=excluded.sponsorship,
    sponsorship_reason=excluded.sponsorship_reason
WHERE jobs.status NOT IN ({_VERDICT_SQL}) OR excluded.status IN ({_VERDICT_SQL})
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


# CREATE TABLE IF NOT EXISTS reaches a brand-new database but never an
# existing one, so a column added to _SCHEMA above stays invisible to the rows
# already in data/coldstart.sqlite3. SQLite's ADD COLUMN is a metadata-only
# operation — O(1), no table rewrite, no data movement — so it is safe to run
# against a live WAL database mid-poll. Existing rows get NULL, which is
# honest: the reason genuinely was not recorded when they were written.
# Do NOT backfill by re-running the classifier; its verdicts have changed, and
# a backfill would file today's answers under yesterday's flags.
_ADDITIVE_COLUMNS: dict[str, dict[str, str]] = {
    "jobs": {
        "location_reason": "TEXT",
        "delist_reason": "TEXT",
        "delisted_at": "TEXT",
        "stack_reason": "TEXT",
        "sponsorship": "TEXT",
        "sponsorship_reason": "TEXT",
    },
}


def _apply_additive_columns(conn: sqlite3.Connection) -> None:
    for table, columns in _ADDITIVE_COLUMNS.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        if not existing:
            continue
        for name, decl in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def _opt(row: sqlite3.Row, key: str):
    """Read a column that may predate the running schema. The dashboard opens
    read-only connections that cannot run the ALTER, and sqlite3.Row raises
    IndexError rather than returning None for an absent key."""
    return row[key] if key in row.keys() else None


def init_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(_SCHEMA)
    _apply_additive_columns(conn)
    conn.commit()


def load_seen_keys(
    conn: sqlite3.Connection,
) -> tuple[set[str], set[tuple[str, str, str]]]:
    # PENDING is excluded on purpose: it means "requeued, not yet processed".
    # The pipeline itself never writes it — only scripts/recheck_held_back.py
    # does, to rescue a job the location filter held back before a lexicon fix.
    # Counting those as seen would make the rescue a no-op, since dedupe drops
    # anything already in `jobs`.
    global_ids = {
        row[0]
        for row in conn.execute("SELECT global_id FROM jobs WHERE status != ?", (_PENDING,))
    }
    # (company, requisition_id, location) — bare requisition_id collides across
    # unrelated companies/postings on real data; see DEVELOPMENT_PLAN.md Module 10.
    req_keys = {
        (row["company"], row["requisition_id"], row["location"])
        for row in conn.execute(
            "SELECT company, requisition_id, location FROM jobs "
            "WHERE requisition_id IS NOT NULL AND status != ?",
            (_PENDING,),
        )
    }
    return global_ids, req_keys


class LegacyGlobalIds(Exception):
    """The database holds jobs keyed the way they were before Module 37."""


def count_legacy_global_ids(conn: sqlite3.Connection) -> int:
    """Stored jobs from a per-employer source still keyed `ats_type:ats_id`,
    without the site host global_ids.make_global_id now puts in the middle.

    No migration exists on purpose: the operator chose to start from an empty
    database (2026-10-20) instead. The count is what run_poll refuses on —
    polling a database like that would treat every one of those jobs as
    unseen and send it to the LLM again."""
    types = sorted(PER_EMPLOYER_ATS_TYPES)
    placeholders = ", ".join("?" * len(types))
    return conn.execute(
        f"SELECT COUNT(*) FROM jobs WHERE ats_type IN ({placeholders}) "
        "AND global_id NOT LIKE ats_type || ':%:%'",
        types,
    ).fetchone()[0]


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


def clear_slice_state(conn: sqlite3.Connection, ats_type: str) -> bool:
    """Forget that this source was ever processed, so the next poll redoes it.

    Module 29's "re-run" action. Deliberately narrow — one row of change
    detection, nothing in `jobs` — and it is not as destructive as it looks:
    the parquet is already on disk and sha256-verified, so there is no
    re-download, and `dedupe` drops every posting already stored, so the
    re-run costs a read and a filter pass and essentially no LLM spend. What
    it is *for* is picking up a lexicon or rule change against a source the
    snapshot hasn't touched since.

    Returns whether a row was actually removed, so a caller can tell "done"
    from "there was nothing to forget"."""
    cursor = conn.execute("DELETE FROM slice_state WHERE ats_type = ?", (ats_type,))
    conn.commit()
    return cursor.rowcount > 0


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


def llm_scored_companies_between(
    conn: sqlite3.Connection, start: str, end: str, *, manual_provider: str
) -> list[str]:
    """The company of every job an LLM scored in [start, end), one entry per
    job. Not filtered on status: a job delisted after it was scored still
    took its company's LLM slot that day (Module 40)."""
    rows = conn.execute(
        "SELECT company FROM jobs WHERE scored_at >= ? AND scored_at < ? "
        "AND provider_used IS NOT NULL AND provider_used != ?",
        (start, end, manual_provider),
    ).fetchall()
    return [row["company"] for row in rows]


def mark_delisted(
    conn: sqlite3.Connection, global_id: str, reason: str, checked_at: datetime
) -> None:
    """Flip an already-SCORED row to DELISTED without touching anything else.

    A dedicated UPDATE rather than upsert_job's full replace: the periodic
    liveness sweep only knows the check outcome, not the job's score/
    reasoning/matched_skills, and upsert_job's ON CONFLICT would need all of
    those re-supplied or it overwrites them with whatever the caller happened
    to pass. This preserves the original score for reference — the row still
    shows what it scored, it just stops being surfaced as an active match."""
    conn.execute(
        "UPDATE jobs SET status = ?, delist_reason = ?, delisted_at = ? WHERE global_id = ?",
        (JobStatus.DELISTED.value, reason, checked_at.isoformat(), global_id),
    )
    conn.commit()


def scored_jobs_for_liveness_check(
    conn: sqlite3.Connection, ats_types: frozenset[str]
) -> list[LivenessCandidate]:
    """SCORED rows on a checkable ats_type that the operator hasn't already
    acted on — the periodic sweep's input set. Excludes anything in
    job_state (applied/declined): there's no point spending a request
    re-verifying a job the operator has already moved past."""
    if not ats_types:
        return []
    placeholders = ",".join("?" * len(ats_types))
    rows = conn.execute(
        f"""
        SELECT j.global_id, j.ats_type, j.apply_url, j.company
        FROM jobs j
        LEFT JOIN job_state s ON s.global_id = j.global_id
        WHERE j.status = 'scored' AND j.ats_type IN ({placeholders}) AND s.global_id IS NULL
        """,
        tuple(ats_types),
    ).fetchall()
    return [
        LivenessCandidate(
            global_id=row["global_id"],
            ats_type=row["ats_type"],
            apply_url=row["apply_url"],
            company=row["company"],
        )
        for row in rows
    ]


def count_unresolved_errors(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT COUNT(*) FROM errors WHERE resolved = 0").fetchone()
    return row[0]


# The only states the dashboard may set. Kept deliberately small — an
# unrecognised value from a request must never reach the database.
# "rejected" is an application the employer turned down (Module 34): still an
# application, and it hides that company for COMPANY_COOLDOWN_DAYS.
JOB_STATES = frozenset({"applied", "declined", "rejected"})

# Why a job was declined, one tap each (Module 34). Stored in job_state.note,
# which existed from the start and was never written. Fixed list, so the
# reasons can be counted later rather than read.
DECLINE_REASONS = frozenset(
    {"company", "role_type", "seniority", "location", "visa", "stack", "already_applied"}
)


def _check_mark(state: str, note: str | None) -> None:
    if state not in JOB_STATES:
        raise ValueError(f"unknown job state {state!r}; expected one of {sorted(JOB_STATES)}")
    if note is not None and (state != "declined" or note not in DECLINE_REASONS):
        raise ValueError(
            f"a note is a decline reason, one of {sorted(DECLINE_REASONS)}; "
            f"got {note!r} for state {state!r}"
        )


def set_job_state(
    conn: sqlite3.Connection, global_id: str, state: str, note: str | None = None
) -> None:
    _check_mark(state, note)
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


def _unique(global_ids: Iterable[str]) -> list[str]:
    """Order-preserving dedupe. A caller passing the same id twice is not an
    error, but writing it twice in one executemany is pointless work."""
    return list(dict.fromkeys(global_ids))


def set_job_states(
    conn: sqlite3.Connection,
    global_ids: Iterable[str],
    state: str,
    note: str | None = None,
) -> int:
    """Mark many jobs at once, in a single transaction. Returns how many.

    Declining a company means declining every posting under it — 117 rows for
    Accenture — and the per-row `set_job_state` commits each one, so doing it
    in a loop is 117 chances to be interrupted with the company half declined.
    One executemany and one commit: the whole group moves or none of it does.

    The state check lives here rather than in the caller for the same reason
    it lives in `set_job_state` — a guard every caller has to remember is not
    a guard."""
    _check_mark(state, note)
    ids = _unique(global_ids)
    if not ids:
        return 0
    now = datetime.now(UTC).isoformat()
    conn.executemany(
        """
        INSERT INTO job_state (global_id, state, updated_at, note)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(global_id) DO UPDATE SET
            state=excluded.state,
            updated_at=excluded.updated_at,
            note=excluded.note
        """,
        [(global_id, state, now, note) for global_id in ids],
    )
    conn.commit()
    return len(ids)


def clear_job_states(conn: sqlite3.Connection, global_ids: Iterable[str]) -> int:
    """The undo half of set_job_states, same all-or-nothing commit."""
    ids = _unique(global_ids)
    if not ids:
        return 0
    conn.executemany(
        "DELETE FROM job_state WHERE global_id = ?", [(global_id,) for global_id in ids]
    )
    conn.commit()
    return len(ids)


def stale_jobs(
    conn: sqlite3.Connection, max_age_days: int, now: pd.Timestamp
) -> list[tuple[sqlite3.Row, float]]:
    """Stored jobs whose posting date is older than `max_age_days`, oldest
    first, each paired with its age in days. Each row carries `mark` — the
    job_state, or None — so the caller decides what an actioned job means.

    Decided by `freshness.is_fresh`, the same function behind the pipeline's
    filter, rather than a `julianday()` compare in SQL: that would have been
    shorter and would have quietly disagreed with pandas on the messier
    timestamps. Undated and future-dated rows are therefore never stale."""
    rows = conn.execute(
        """
        SELECT j.global_id, j.company, j.title, j.posted_at, j.status, s.state AS mark
        FROM jobs j
        LEFT JOIN job_state s ON s.global_id = j.global_id
        WHERE j.posted_at IS NOT NULL AND j.posted_at <> ''
        """
    ).fetchall()
    stale = [
        (row, posting_age_days(row["posted_at"], now) or 0.0)
        for row in rows
        if not is_fresh(row["posted_at"], now, max_age_days)[0]
    ]
    return sorted(stale, key=lambda pair: -pair[1])


def delete_jobs(
    conn: sqlite3.Connection, global_ids: Iterable[str], *, with_marks: bool = False
) -> int:
    """Delete jobs outright, in one transaction. Returns how many were asked for.

    `with_marks` deletes their job_state rows too. Pass it whenever a deleted
    job might be marked: nothing enforces that link at the schema level, and a
    mark whose job is gone is unreadable, not a record."""
    ids = [(global_id,) for global_id in _unique(global_ids)]
    if not ids:
        return 0
    conn.executemany("DELETE FROM jobs WHERE global_id = ?", ids)
    if with_marks:
        conn.executemany("DELETE FROM job_state WHERE global_id = ?", ids)
    conn.commit()
    return len(ids)


def job_states(conn: sqlite3.Connection) -> dict[str, str]:
    """Every marked job, as {global_id: state}.

    One query for the whole set rather than a join per listing: at these
    volumes it's a few hundred rows at most, and it keeps the jobs query
    itself unchanged."""
    return {row["global_id"]: row["state"] for row in conn.execute(
        "SELECT global_id, state FROM job_state"
    )}


def job_state_rows(conn: sqlite3.Connection) -> dict[str, sqlite3.Row]:
    """Every mark with its date and note, as {global_id: row}. job_states()
    is the state alone, for the many callers that need nothing else."""
    return {row["global_id"]: row for row in conn.execute(
        "SELECT global_id, state, updated_at, note FROM job_state"
    )}


def block_company(conn: sqlite3.Connection, company_key: str, company: str) -> None:
    """"Not this company": every posting from it skips the LLM until undone."""
    if not company_key:
        raise ValueError("company_key is empty")
    conn.execute(
        """
        INSERT INTO company_state (company_key, company, state, updated_at)
        VALUES (?, ?, 'blocked', ?)
        ON CONFLICT(company_key) DO UPDATE SET
            company=excluded.company, state=excluded.state, updated_at=excluded.updated_at
        """,
        (company_key, company, datetime.now(UTC).isoformat()),
    )
    conn.commit()


def unblock_company(conn: sqlite3.Connection, company_key: str) -> bool:
    cursor = conn.execute("DELETE FROM company_state WHERE company_key = ?", (company_key,))
    conn.commit()
    return cursor.rowcount > 0


def company_states(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT company_key, company, state, updated_at FROM company_state ORDER BY updated_at DESC"
    ).fetchall()


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


def queue_hemline_event(
    conn: sqlite3.Connection, global_id: str, event_id: str, payload: str
) -> bool:
    """Record a job as owed to hemline. False if it already has a row — a job
    is queued once, ever, whatever happens to it afterwards."""
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO hemline_events
            (global_id, event_id, payload, status, attempts, created_at)
        VALUES (?, ?, ?, 'pending', 0, ?)
        """,
        (global_id, event_id, payload, datetime.now(UTC).isoformat()),
    )
    conn.commit()
    return cursor.rowcount == 1


def get_hemline_event(conn: sqlite3.Connection, global_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM hemline_events WHERE global_id = ?", (global_id,)
    ).fetchone()


def pending_hemline_events(conn: sqlite3.Connection) -> list[str]:
    """Oldest first, so a backlog drains in the order the jobs were scored."""
    return [
        row["global_id"]
        for row in conn.execute(
            "SELECT global_id FROM hemline_events WHERE status = 'pending' "
            "ORDER BY created_at, global_id"
        )
    ]


def mark_hemline_sent(conn: sqlite3.Connection, global_id: str) -> None:
    conn.execute(
        """
        UPDATE hemline_events
        SET status = 'sent', attempts = attempts + 1, last_error = NULL, sent_at = ?
        WHERE global_id = ? AND status = 'pending'
        """,
        (datetime.now(UTC).isoformat(), global_id),
    )
    conn.commit()


def record_hemline_failure(
    conn: sqlite3.Connection, global_id: str, error: str, *, give_up: bool
) -> None:
    conn.execute(
        """
        UPDATE hemline_events
        SET attempts = attempts + 1, last_error = ?,
            status = CASE WHEN ? THEN 'failed' ELSE status END
        WHERE global_id = ? AND status = 'pending'
        """,
        (error, give_up, global_id),
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
        job.location_reason,
        job.eligibility_flag.value,
        job.provider_used,
        job.first_seen_at.isoformat(),
        job.scored_at.isoformat() if job.scored_at else None,
        job.delist_reason,
        job.delisted_at.isoformat() if job.delisted_at else None,
        job.stack_reason,
        job.sponsorship.value if job.sponsorship else None,
        job.sponsorship_reason,
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
        location_reason=_opt(row, "location_reason"),
        eligibility_flag=row["eligibility_flag"],
        provider_used=row["provider_used"],
        first_seen_at=row["first_seen_at"],
        scored_at=row["scored_at"],
        delist_reason=_opt(row, "delist_reason"),
        delisted_at=_opt(row, "delisted_at"),
        stack_reason=_opt(row, "stack_reason"),
        sponsorship=_opt(row, "sponsorship"),
        sponsorship_reason=_opt(row, "sponsorship_reason"),
    )
