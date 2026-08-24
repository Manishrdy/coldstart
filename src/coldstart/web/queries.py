"""Read-only queries backing the dashboard.

Nothing here writes. Every caller passes a `mode=ro` connection from
`db.readonly_connection`, so a dashboard request physically cannot lock or
mutate the database while run_poll is committing to it.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from statistics import median

from coldstart.budget import local_day_bounds_utc, today_spend
from coldstart.db import (
    count_unresolved_errors,
    job_states,
    providers_used_since,
    run_totals_since,
)
from coldstart.settings import Settings

# The dashboard is a working view, not an archive; a cap keeps one runaway
# query from serializing the whole table into a browser tab. Pagination can
# come later if the scored set ever gets near this.
_ROW_LIMIT = 5000

_JOB_COLUMNS = """
    global_id, requisition_id, company, title, location, apply_url, ats_type,
    posted_at, resume_used, score, score_band, eligible, matched_skills,
    missing_skills, reasoning, status, location_flag, location_reason,
    eligibility_flag, provider_used, first_seen_at, scored_at
"""


def band_for(score: int, settings: Settings) -> str:
    """Band a score the way the digest does — from the configured thresholds.

    Deliberately NOT `jobs.score_band`. The rubric prompt never tells the model
    what the operator's thresholds are (scope.md §8, Module 18), so the stored
    band is the LLM's own opinion and changing SCORE_THRESHOLD_* in .env would
    silently fail to move anything if we trusted it. The dashboard has to agree
    with the email, so it bands the same way the email does."""
    if score >= settings.score_threshold_strong:
        return "strong"
    if score >= settings.score_threshold_consider:
        return "consider"
    return "reject"


def _decode_skills(value: str | None) -> list[str]:
    # db.py stores these as JSON TEXT. (export.py deliberately uses "; " joins
    # instead, for spreadsheet friendliness — the dashboard follows db.py.)
    if not value:
        return []
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return []
    return [str(item) for item in decoded] if isinstance(decoded, list) else []


def _row_to_dict(row: sqlite3.Row, settings: Settings, state: str | None = None) -> dict:
    score = row["score"]
    return {
        # Your decision about this job, from the job_state table — never from
        # `jobs`, which the pipeline rewrites.
        "state": state,
        "global_id": row["global_id"],
        "requisition_id": row["requisition_id"],
        "company": row["company"],
        "title": row["title"],
        "location": row["location"],
        "apply_url": row["apply_url"],
        "ats_type": row["ats_type"],
        "posted_at": row["posted_at"],
        "resume_used": row["resume_used"],
        "score": score,
        # Held-back rows never reached an LLM, so score is NULL and band_for
        # would raise on the comparison.
        "band": band_for(score, settings) if score is not None else None,
        # The LLM's self-assigned band, surfaced separately so a disagreement
        # with `band` is visible rather than hidden.
        "llm_band": row["score_band"],
        "eligible": None if row["eligible"] is None else bool(row["eligible"]),
        "matched_skills": _decode_skills(row["matched_skills"]),
        "missing_skills": _decode_skills(row["missing_skills"]),
        "reasoning": row["reasoning"],
        "location_flag": row["location_flag"],
        "location_reason": row["location_reason"],
        "eligibility_flag": row["eligibility_flag"],
        "provider_used": row["provider_used"],
        "first_seen_at": row["first_seen_at"],
        "scored_at": row["scored_at"],
    }


def list_jobs(
    conn: sqlite3.Connection, settings: Settings, *, include_reject: bool = False
) -> list[dict]:
    """Scored jobs, non-reject by default.

    `status='scored'` is the real predicate for "has a score at all" — the
    other statuses are `excluded` (eligibility-filtered, persisted for audit)
    and `failed`. Filtering on `score_band != 'reject'` instead would sweep in
    every NULL-band excluded row."""
    threshold = -1 if include_reject else settings.score_threshold_consider
    rows = conn.execute(
        f"""
        SELECT {_JOB_COLUMNS}
        FROM jobs
        WHERE status = 'scored' AND score IS NOT NULL AND score >= ?
        ORDER BY score DESC, scored_at DESC
        LIMIT ?
        """,
        (threshold, _ROW_LIMIT),
    ).fetchall()
    states = job_states(conn)
    return [_row_to_dict(row, settings, states.get(row["global_id"])) for row in rows]


def list_location_excluded(
    conn: sqlite3.Connection, settings: Settings, *, limit: int = 500
) -> list[dict]:
    """Jobs the location filter held back, newest first.

    These never reached an LLM, so score/band/reasoning are all NULL — the
    column that matters is `location_reason`, which names the rule that fired.
    A reason of "unresolved" or "bare_remote" means no rule fired at all and
    the lexicon has a gap; a recognisable US city showing up here repeatedly
    is the signal to extend config/us_cities.json.

    Capped well below _ROW_LIMIT: this bucket grows faster than the scored one
    (roughly one held-back row per 1.5 scored) and nobody reviews 5,000 rows."""
    rows = conn.execute(
        f"""
        SELECT {_JOB_COLUMNS}
        FROM jobs
        WHERE status = 'excluded_location'
        ORDER BY first_seen_at DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    states = job_states(conn)
    return [_row_to_dict(row, settings, states.get(row["global_id"])) for row in rows]


def metrics(conn: sqlite3.Connection, settings: Settings) -> dict:
    since_str, _until_str = local_day_bounds_utc(settings.timezone)
    since = datetime.fromisoformat(since_str)

    scored = conn.execute(
        "SELECT score, company, COALESCE(scored_at, first_seen_at) AS seen_at "
        "FROM jobs WHERE status = 'scored' AND score IS NOT NULL"
    ).fetchall()

    bands = {"strong": 0, "consider": 0, "reject": 0}
    kept_scores: list[int] = []
    kept_companies: set[str] = set()
    new_today = 0
    for row in scored:
        band = band_for(row["score"], settings)
        bands[band] += 1
        if band == "reject":
            continue
        kept_scores.append(row["score"])
        kept_companies.add(row["company"])
        if row["seen_at"] and row["seen_at"] >= since_str:
            new_today += 1

    totals = run_totals_since(conn, since)

    slice_row = conn.execute(
        "SELECT COUNT(*) AS n, MAX(last_processed_at) AS latest FROM slice_state"
    ).fetchone()
    email_row = conn.execute(
        "SELECT sent_at, job_count FROM email_log "
        "WHERE status = 'sent' AND sent_at >= ? ORDER BY sent_at DESC LIMIT 1",
        (since_str,),
    ).fetchone()

    states = job_states(conn)
    applied = sum(1 for state in states.values() if state == "applied")

    held_back = conn.execute(
        "SELECT COUNT(*) AS n FROM jobs WHERE status = 'excluded_location'"
    ).fetchone()["n"]

    return {
        "applied": applied,
        "held_back": held_back,
        "total": len(kept_scores),
        "strong": bands["strong"],
        "consider": bands["consider"],
        "reject": bands["reject"],
        "new_today": new_today,
        "today_since": since_str,
        "companies": len(kept_companies),
        "median_score": round(median(kept_scores), 1) if kept_scores else None,
        "max_score": max(kept_scores) if kept_scores else None,
        "funnel": totals,
        "spend_today_usd": today_spend(conn, settings.timezone),
        "providers_used": providers_used_since(conn, since),
        "unresolved_errors": count_unresolved_errors(conn),
        "slices_tracked": slice_row["n"],
        "slice_last_processed_at": slice_row["latest"],
        "digest_sent_at": email_row["sent_at"] if email_row else None,
        "digest_job_count": email_row["job_count"] if email_row else None,
        "thresholds": {
            "strong": settings.score_threshold_strong,
            "consider": settings.score_threshold_consider,
        },
    }


def data_version(conn: sqlite3.Connection) -> str:
    """A cheap token that changes whenever the jobs table does.

    Counts catch inserts; the max timestamp catches a re-score of a row that
    already existed (upsert_job keeps first_seen_at but moves scored_at)."""
    row = conn.execute(
        "SELECT COUNT(*) AS n, MAX(COALESCE(scored_at, first_seen_at)) AS latest FROM jobs"
    ).fetchone()
    return f"{row['n']}:{row['latest'] or '-'}"
