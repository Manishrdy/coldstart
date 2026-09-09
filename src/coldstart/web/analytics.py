"""Module 28 — every number the pipeline knows about itself, in one payload.

The dashboard's six tiles answer "what should I look at today". This answers
a different question: **is the machine healthy, and what has it actually
done.** Six months of runs, spend, sources, filter verdicts and errors are
already in the database; almost none of it was ever readable without opening
sqlite3 by hand.

Three rules this module follows, all of them lessons already paid for
elsewhere in the project:

1. **Read-only, always.** Same contract as queries.py: every caller passes a
   `mode=ro` connection, so nothing here can lock or mutate the database
   while run_poll is committing to it.

2. **Days are the operator's days, not UTC's.** Timestamps persist as UTC ISO
   strings, and grouping them with `substr(ts, 1, 10)` would cut the day at
   17:00 local — an evening's work would land on tomorrow. Every per-day
   bucket here converts through `settings.timezone` first, so these charts
   agree with the digest window and the spend ceiling, which both already
   work in local days (budget.local_day_bounds_utc).

3. **A number that cannot be trusted says so.** `est_cost_usd` is 0 for every
   row when the active model has no PRICING entry — reporting "$0.00 spent"
   as if it were measured would be a lie. The payload carries `priced` next
   to the cost so the page can show tokens, which are always real, and label
   the dollars as unavailable rather than zero.
"""

from __future__ import annotations

import json
import sqlite3
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from coldstart.ats_control import order_slices, read_control
from coldstart.budget import local_day_bounds_utc
from coldstart.db import get_daemon_state
from coldstart.models import JobStatus
from coldstart.progress import read_progress
from coldstart.scoring.providers import PRICING
from coldstart.settings import Settings
from coldstart.verify import CHECKED_ATS_TYPES
from coldstart.web.queries import band_for

# How far back the time-series charts reach. Long enough to see a weekly
# rhythm, short enough that the payload stays a few KB.
_TIMELINE_DAYS = 30
_HOURLY_HOURS = 48

# Table caps. These are lists a person reads, not data they export — the CSV
# button on the main dashboard is for export.
_TOP_N = 15
_RECENT_ERRORS = 25
_RECENT_DIGESTS = 20
_RECENT_RUNS = 15

_COUNTED_TABLES = (
    "jobs", "job_state", "run_log", "spend_log", "email_log", "errors", "slice_state",
)

_MANIFEST_BODY_KEY = "manifest_body"
_LIVENESS_SWEEP_KEY = "liveness_sweep_last_run_at"

# Slices deliberately never fetched (config/excluded_ats.json) — resolved the
# same repo-relative way pipeline.py resolves it, since there is no setting
# for this path.
_EXCLUDED_ATS_PATH = (
    Path(__file__).resolve().parent.parent.parent.parent / "config" / "excluded_ats.json"
)


# --- small helpers ---------------------------------------------------------


def _parse(value: str | None) -> datetime | None:
    """Parse a stored timestamp, tolerating anything unexpected.

    `posted_at` originates in a third-party parquet file rather than from our
    own `datetime.now(UTC).isoformat()`, so it can be naive or malformed. A
    bad date must degrade one row, never the whole page."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed.replace(tzinfo=UTC) if parsed.tzinfo is None else parsed


def _local_day(value: str | None, tz: ZoneInfo) -> str | None:
    parsed = _parse(value)
    return None if parsed is None else parsed.astimezone(tz).date().isoformat()


def _local_hour(value: str | None, tz: ZoneInfo) -> str | None:
    parsed = _parse(value)
    return None if parsed is None else parsed.astimezone(tz).strftime("%Y-%m-%dT%H")


def _day_axis(tz: ZoneInfo, days: int) -> list[str]:
    """A dense list of the last `days` local dates, oldest first.

    Dense on purpose: a chart built only from days that have rows draws a
    quiet week as a straight line instead of as the gap it was."""
    today = datetime.now(UTC).astimezone(tz).date()
    return [(today - timedelta(days=offset)).isoformat() for offset in range(days - 1, -1, -1)]


def _hour_axis(tz: ZoneInfo, hours: int) -> list[str]:
    now = datetime.now(UTC).astimezone(tz).replace(minute=0, second=0, microsecond=0)
    return [
        (now - timedelta(hours=offset)).strftime("%Y-%m-%dT%H")
        for offset in range(hours - 1, -1, -1)
    ]


def _daily_series(counts: Counter, tz: ZoneInfo, days: int = _TIMELINE_DAYS) -> list[dict]:
    """A dense {day, count} series off a Counter keyed by local date."""
    return [{"day": day, "count": counts.get(day, 0)} for day in _day_axis(tz, days)]


def _percentile(sorted_values: list[int], fraction: float) -> float | None:
    """Nearest-rank percentile. SQLite has no percentile function, and the
    scored set is small enough to sort in memory."""
    if not sorted_values:
        return None
    index = max(0, min(len(sorted_values) - 1, round(fraction * (len(sorted_values) - 1))))
    return float(sorted_values[index])


def _counter_rows(counter: Counter, *, limit: int | None = None) -> list[dict]:
    items = counter.most_common(limit)
    return [{"key": "—" if key is None else str(key), "count": count} for key, count in items]


def _file_size(path: Path) -> int | None:
    try:
        return path.stat().st_size
    except OSError:
        return None


def _decode_skills(value: str | None) -> list[str]:
    if not value:
        return []
    try:
        decoded = json.loads(value)
    except (TypeError, ValueError):
        return []
    return [str(item) for item in decoded] if isinstance(decoded, list) else []


def _load_manifest(conn: sqlite3.Connection) -> dict:
    """The manifest the daemon last downloaded, from its own cache.

    Reading the cached copy rather than re-fetching is the whole point: the
    dashboard must never make a network call to render, and this is exactly
    the snapshot the poll is working against right now."""
    raw = get_daemon_state(conn, _MANIFEST_BODY_KEY)
    if not raw:
        return {}
    try:
        manifest = json.loads(raw)
    except ValueError:
        return {}
    by_ats = manifest.get("by_ats")
    return by_ats if isinstance(by_ats, dict) else {}


def _load_excluded_ats() -> set[str]:
    try:
        entries = json.loads(_EXCLUDED_ATS_PATH.read_text())
    except (OSError, ValueError):
        return set()
    return {entry["name"] for entry in entries if isinstance(entry, dict) and "name" in entry}


# --- sections --------------------------------------------------------------


def config_summary(settings: Settings) -> dict:
    """The settings that shape every number on the page.

    Deliberately no secrets: API keys, SMTP password and the recipient
    address are all absent. Everything here is a knob whose value explains a
    number elsewhere on the page — if the strong count looks wrong, the
    threshold that produced it is right there."""
    model = getattr(settings, f"{settings.llm_provider}_model", None)
    return {
        "timezone": settings.timezone,
        "score_threshold_strong": settings.score_threshold_strong,
        "max_posting_age_days": settings.max_posting_age_days,
        "poll_interval_minutes": settings.poll_interval_minutes,
        "force_poll_hours": settings.force_poll_hours,
        "poll_timeout_minutes": settings.poll_timeout_minutes,
        "digest_time": settings.digest_time_pdt,
        "llm_mode": settings.llm_mode,
        "llm_provider": settings.llm_provider if settings.llm_mode == "prod" else "ollama",
        "llm_model": model or (settings.ollama_model if settings.llm_mode == "dev" else None),
        "experience_years": settings.experience_years,
        "daily_spend_ceiling_usd": settings.daily_token_spend_ceiling_usd,
        "liveness_check_enabled": settings.liveness_check_enabled,
        "liveness_sweep_interval_hours": settings.liveness_sweep_interval_hours,
        "checked_ats_types": sorted(CHECKED_ATS_TYPES),
        "db_path": str(settings.db_path),
    }


def database(conn: sqlite3.Connection, settings: Settings) -> dict:
    """How much is in here, and how far back it goes."""
    by_status = Counter()
    for row in conn.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status"):
        by_status[row["status"]] = row["n"]

    spans = conn.execute(
        """
        SELECT COUNT(*) AS total,
               COUNT(DISTINCT company)  AS companies,
               COUNT(DISTINCT ats_type) AS ats_types,
               MIN(first_seen_at) AS first_seen,
               MAX(first_seen_at) AS last_seen,
               MAX(scored_at)     AS last_scored
        FROM jobs
        """
    ).fetchone()

    marks = Counter(
        row["state"]
        for row in conn.execute("SELECT state FROM job_state")
    )

    db_path = Path(settings.db_path)
    return {
        "total_jobs": spans["total"],
        "by_status": {status.value: by_status.get(status.value, 0) for status in JobStatus},
        "companies": spans["companies"],
        "ats_types": spans["ats_types"],
        "first_seen_at": spans["first_seen"],
        "last_seen_at": spans["last_seen"],
        "last_scored_at": spans["last_scored"],
        "applied": marks.get("applied", 0),
        "declined": marks.get("declined", 0),
        "size_bytes": _file_size(db_path),
        "wal_bytes": _file_size(db_path.with_name(db_path.name + "-wal")),
        "tables": {
            name: conn.execute(f"SELECT COUNT(*) AS n FROM {name}").fetchone()["n"]
            for name in _COUNTED_TABLES
        },
    }


def live(conn: sqlite3.Connection, settings: Settings) -> dict:
    """What the pipeline is doing this second.

    Two independent sources, kept separate on purpose. `progress` comes from
    the poll subprocess's own heartbeat (progress.py) and is the only thing
    that knows *which slice*; the daemon's in-memory state (merged in by the
    endpoint) knows the schedule. Neither can see what the other sees."""
    progress = read_progress(conn)
    work = queue(conn, settings)

    payload: dict = {
        "relevant_slices": work["total"],
        "outstanding_slices": work["queued"],
        "queue": work,
        "manifest_known": work["manifest_known"],
        "progress": None,
    }

    if progress is not None:
        payload["progress"] = {
            **progress.model_dump(mode="json"),
            "is_running": progress.is_running,
            "age_seconds": round(progress.age_seconds, 1),
            # A run whose heartbeat stopped without a `finished_at` was killed
            # — the timeout, or the machine going down mid-slice. Naming it
            # here means the page never has to infer it.
            "was_killed": progress.finished_at is None and not progress.is_running,
        }
    return payload


def _empty_yield() -> dict:
    return {
        "total": 0, "by_status": {}, "score_sum": 0.0, "score_n": 0,
        "max_score": None, "last_seen": None,
    }


def relevant_sources(conn: sqlite3.Connection) -> dict[str, dict]:
    """Every source the poll would consider: in the manifest, not excluded by
    config, and not empty. The same three conditions manifest_watch applies —
    kept in one place here so the queue and the sources table agree."""
    excluded = _load_excluded_ats()
    return {
        ats_type: entry
        for ats_type, entry in _load_manifest(conn).items()
        if ats_type not in excluded and (entry.get("rows") or 0)
    }


def queue(conn: sqlite3.Connection, settings: Settings) -> dict:
    """Every source the poll can run, in the order it will actually run them.

    Module 29. Before this, "the work queue" was a derived fact nobody could
    influence: `changed_slices` handed back whatever the manifest listed, in
    the manifest's own order, and `workday` at 839k rows meant everything
    behind it waited hours. The order is now the operator's to set, so it has
    to be *shown* — a priority you cannot see is a priority you cannot trust.

    `position` is the real running order, taken from the same `order_slices`
    the pipeline uses, not a re-implementation of it."""
    tz = ZoneInfo(settings.timezone)
    control = read_control(conn)
    entries = relevant_sources(conn)

    state = {
        row["ats_type"]: row
        for row in conn.execute("SELECT ats_type, last_sha256, last_processed_at FROM slice_state")
    }
    progress = read_progress(conn)
    running = (
        progress.ats_type if progress is not None and progress.is_running else None
    )

    def _outstanding(ats_type: str, entry: dict) -> bool:
        sha = entry.get("parquet_sha256") or entry.get("sha256")
        known = state.get(ats_type)
        return known is None or known["last_sha256"] != sha

    outstanding = [ats for ats, entry in entries.items() if _outstanding(ats, entry)]
    # Held sources are split out by order_slices exactly as the pipeline does
    # it, so what the page numbers is what the poll will do.
    runnable, _held = order_slices(
        sorted(outstanding), control, key=lambda ats_type: ats_type
    )
    positions = {ats_type: index for index, ats_type in enumerate(runnable, start=1)}

    rows = []
    for ats_type in sorted(entries):
        entry = entries[ats_type]
        known = state.get(ats_type)
        is_held = control.is_held(ats_type)
        if ats_type == running:
            status = "running"
        elif is_held:
            status = "held"
        elif ats_type in positions:
            status = "queued"
        else:
            status = "up_to_date"

        rows.append(
            {
                "ats_type": ats_type,
                "status": status,
                # None for anything not in the run order — held, up to date,
                # or the one already running.
                "position": positions.get(ats_type),
                "held": is_held,
                "prioritised": ats_type in control.priority,
                "priority_rank": control.priority.index(ats_type) + 1
                if ats_type in control.priority
                else None,
                "outstanding": ats_type in outstanding,
                "never_processed": known is None,
                "rows": entry.get("rows") or 0,
                "size_bytes": entry.get("parquet_size_bytes") or entry.get("size_bytes") or 0,
                "last_processed_at": known["last_processed_at"] if known else None,
                "last_processed_day": _local_day(
                    known["last_processed_at"] if known else None, tz
                ),
                "liveness_checkable": ats_type in CHECKED_ATS_TYPES,
            }
        )

    # Running first, then the queue in order, then up-to-date, then held.
    rank = {"running": 0, "queued": 1, "up_to_date": 2, "held": 3}
    rows.sort(key=lambda row: (rank[row["status"]], row["position"] or 0, row["ats_type"]))

    return {
        "rows": rows,
        "running": running,
        "queued": len(runnable),
        "held": sorted(control.held),
        "priority": list(control.priority),
        "total": len(rows),
        "manifest_known": bool(entries),
        "has_overrides": bool(control.priority or control.held),
    }


def sources(conn: sqlite3.Connection, settings: Settings) -> dict:
    """One row per ATS: when it was last processed, and what it yielded.

    The question "when was greenhouse last done" had two half-answers before
    this — `slice_state.last_processed_at` knew the *when* and `jobs` knew the
    *what* — and nothing joined them. This does, and folds in the manifest so
    a source that has never been processed at all still appears (with a null
    date) instead of silently missing."""
    tz = ZoneInfo(settings.timezone)
    manifest = _load_manifest(conn)
    excluded = _load_excluded_ats()

    state = {
        row["ats_type"]: row
        for row in conn.execute("SELECT * FROM slice_state")
    }

    yields: dict[str, dict] = {}
    for row in conn.execute(
        """
        SELECT ats_type, status, COUNT(*) AS n,
               AVG(score) AS avg_score, MAX(score) AS max_score,
               MAX(first_seen_at) AS last_seen
        FROM jobs GROUP BY ats_type, status
        """
    ):
        entry = yields.setdefault(row["ats_type"], _empty_yield())
        entry["total"] += row["n"]
        entry["by_status"][row["status"]] = row["n"]
        if row["avg_score"] is not None:
            entry["score_sum"] += row["avg_score"] * row["n"]
            entry["score_n"] += row["n"]
        if row["max_score"] is not None:
            entry["max_score"] = max(entry["max_score"] or 0, row["max_score"])
        previous = entry["last_seen"]
        if row["last_seen"] and (previous is None or row["last_seen"] > previous):
            entry["last_seen"] = row["last_seen"]

    shortlisted = Counter()
    strong = Counter()
    for row in conn.execute(
        "SELECT ats_type, score FROM jobs WHERE status = 'scored' AND score IS NOT NULL"
    ):
        band = band_for(row["score"], settings)
        if band != "reject":
            shortlisted[row["ats_type"]] += 1
        if band == "strong":
            strong[row["ats_type"]] += 1

    rows: list[dict] = []
    for ats_type in sorted(set(manifest) | set(state) | set(yields)):
        entry = manifest.get(ats_type) or {}
        manifest_sha = entry.get("parquet_sha256") or entry.get("sha256")
        slice_row = state.get(ats_type)
        job_stats = yields.get(ats_type) or _empty_yield()
        by_status = job_stats["by_status"]

        is_excluded = ats_type in excluded
        last_sha = slice_row["last_sha256"] if slice_row else None
        last_processed = slice_row["last_processed_at"] if slice_row else None
        if is_excluded:
            status = "excluded_by_config"
        elif not entry:
            status = "not_in_manifest"
        elif slice_row is None:
            status = "never_processed"
        elif manifest_sha and last_sha == manifest_sha:
            status = "up_to_date"
        else:
            status = "outstanding"

        rows.append(
            {
                "ats_type": ats_type,
                "status": status,
                "excluded_by_config": is_excluded,
                "liveness_checkable": ats_type in CHECKED_ATS_TYPES,
                "manifest_rows": entry.get("rows") or 0,
                "manifest_size_bytes": entry.get("parquet_size_bytes")
                or entry.get("size_bytes")
                or 0,
                "snapshot_sha": (manifest_sha or "")[:12] or None,
                "last_processed_at": last_processed,
                "last_processed_day": _local_day(last_processed, tz),
                "processed_rows": slice_row["row_count"] if slice_row else None,
                "jobs": job_stats["total"],
                "scored": by_status.get(JobStatus.SCORED.value, 0),
                "shortlisted": shortlisted.get(ats_type, 0),
                "strong": strong.get(ats_type, 0),
                "held_back": by_status.get(JobStatus.EXCLUDED_LOCATION.value, 0),
                "excluded": by_status.get(JobStatus.EXCLUDED.value, 0),
                "stack_excluded": by_status.get(JobStatus.EXCLUDED_STACK.value, 0),
                "delisted": by_status.get(JobStatus.DELISTED.value, 0),
                "failed": by_status.get(JobStatus.FAILED.value, 0),
                "avg_score": round(job_stats["score_sum"] / job_stats["score_n"], 1)
                if job_stats["score_n"]
                else None,
                "max_score": job_stats["max_score"],
                "last_job_at": job_stats["last_seen"],
            }
        )

    rows.sort(key=lambda item: (-item["jobs"], item["ats_type"]))
    return {
        "rows": rows,
        "manifest_known": bool(manifest),
        "excluded_count": len(excluded),
        "counts": dict(Counter(row["status"] for row in rows)),
    }


def funnel(conn: sqlite3.Connection, settings: Settings) -> dict:
    """Two funnels, because neither one alone is honest.

    The **run** funnel comes from run_log and is the only place fetched and
    filtered counts exist at all — those rows are deliberately never persisted
    to `jobs` (the title/location-rejected volume is enormous; see db.py). But
    it only has rows for runs that reached their end, so a database can hold
    thousands of scored jobs and an empty run_log.

    The **persisted** funnel is derived from `jobs` and is always available —
    it just starts one stage later, at "survived the cheap filters"."""
    since_str, _until = local_day_bounds_utc(settings.timezone)
    week_ago = (datetime.now(UTC) - timedelta(days=7)).isoformat()

    def totals(where: str, params: tuple) -> dict:
        row = conn.execute(
            f"""
            SELECT COUNT(*) AS runs,
                   COALESCE(SUM(fetched_count), 0)  AS fetched,
                   COALESCE(SUM(filtered_count), 0) AS filtered,
                   COALESCE(SUM(scored_count), 0)   AS scored,
                   COALESCE(SUM(failed_count), 0)   AS failed
            FROM run_log {where}
            """,
            params,
        ).fetchone()
        return dict(row)

    recent = [
        dict(row)
        for row in conn.execute(
            "SELECT run_id, started_at, finished_at, fetched_count, filtered_count, "
            "scored_count, failed_count FROM run_log ORDER BY started_at DESC LIMIT ?",
            (_RECENT_RUNS,),
        )
    ]
    for run in recent:
        started, finished = _parse(run["started_at"]), _parse(run["finished_at"])
        run["duration_seconds"] = (
            round((finished - started).total_seconds()) if started and finished else None
        )

    by_status = {
        row["status"]: row["n"]
        for row in conn.execute("SELECT status, COUNT(*) AS n FROM jobs GROUP BY status")
    }
    scored_rows = conn.execute(
        "SELECT score FROM jobs WHERE status = 'scored' AND score IS NOT NULL"
    ).fetchall()
    bands = Counter(band_for(row["score"], settings) for row in scored_rows)
    held_back = by_status.get(JobStatus.EXCLUDED_LOCATION.value, 0)
    eligibility_excluded = by_status.get(JobStatus.EXCLUDED.value, 0)
    stack_excluded = by_status.get(JobStatus.EXCLUDED_STACK.value, 0)
    delisted = by_status.get(JobStatus.DELISTED.value, 0)

    return {
        "runs": {
            "today": totals("WHERE started_at >= ?", (since_str,)),
            "week": totals("WHERE started_at >= ?", (week_ago,)),
            "all": totals("", ()),
        },
        "recent_runs": recent,
        "persisted": [
            {"stage": "Held back (location)", "count": held_back},
            {"stage": "Excluded (eligibility)", "count": eligibility_excluded},
            {"stage": "Excluded (stack/experience)", "count": stack_excluded},
            {"stage": "Delisted (gone at source)", "count": delisted},
            {"stage": "Scored", "count": len(scored_rows)},
            {"stage": "Shortlisted", "count": bands["strong"]},
            {"stage": "Strong", "count": bands["strong"]},
        ],
    }


def scoring(conn: sqlite3.Connection, settings: Settings) -> dict:
    """The distribution of what the model actually said."""
    tz = ZoneInfo(settings.timezone)
    rows = conn.execute(
        """
        SELECT score, score_band, resume_used, eligible, scored_at,
               matched_skills, missing_skills
        FROM jobs WHERE status = 'scored' AND score IS NOT NULL
        """
    ).fetchall()

    scores = sorted(row["score"] for row in rows)
    histogram = [
        {"bucket": bucket, "label": f"{bucket}–{bucket + 9}", "count": 0, "band": ""}
        for bucket in range(0, 100, 10)
    ]
    bands = Counter()
    per_resume: dict[str, dict] = {}
    disagreements = 0
    eligible_true = 0
    matched = Counter()
    missing = Counter()
    per_day_band: dict[str, Counter] = {}

    for row in rows:
        score = row["score"]
        band = band_for(score, settings)
        bands[band] += 1
        histogram[min(score // 10, 9)]["count"] += 1

        if row["score_band"] and row["score_band"] != band:
            disagreements += 1
        if row["eligible"]:
            eligible_true += 1

        slot = row["resume_used"] or "—"
        entry = per_resume.setdefault(
            slot, {"resume": slot, "count": 0, "sum": 0, "max": 0, "shortlisted": 0}
        )
        entry["count"] += 1
        entry["sum"] += score
        entry["max"] = max(entry["max"], score)
        if band != "reject":
            entry["shortlisted"] += 1

        for skill in _decode_skills(row["matched_skills"]):
            matched[skill] += 1
        for skill in _decode_skills(row["missing_skills"]):
            missing[skill] += 1

        day = _local_day(row["scored_at"], tz)
        if day:
            per_day_band.setdefault(day, Counter())[band] += 1

    for bucket in histogram:
        # Band the bucket by its own floor, so the colour of a bar means the
        # same thing as the pill on a row with that score.
        bucket["band"] = band_for(bucket["bucket"], settings)

    axis = _day_axis(tz, _TIMELINE_DAYS)
    return {
        "count": len(scores),
        "histogram": histogram,
        "bands": {band: bands.get(band, 0) for band in ("strong", "reject")},
        "min": scores[0] if scores else None,
        "max": scores[-1] if scores else None,
        "mean": round(sum(scores) / len(scores), 1) if scores else None,
        "p25": _percentile(scores, 0.25),
        "median": _percentile(scores, 0.50),
        "p75": _percentile(scores, 0.75),
        "p90": _percentile(scores, 0.90),
        "llm_band_disagreements": disagreements,
        "eligible": eligible_true,
        "ineligible": len(rows) - eligible_true,
        "by_resume": sorted(
            (
                {
                    **entry,
                    "avg": round(entry["sum"] / entry["count"], 1) if entry["count"] else None,
                }
                for entry in per_resume.values()
            ),
            key=lambda item: item["resume"],
        ),
        "top_matched_skills": _counter_rows(matched, limit=_TOP_N),
        "top_missing_skills": _counter_rows(missing, limit=_TOP_N),
        "per_day": [
            {
                "day": day,
                "strong": per_day_band.get(day, Counter()).get("strong", 0),
                "reject": per_day_band.get(day, Counter()).get("reject", 0),
            }
            for day in axis
        ],
    }


def spend(conn: sqlite3.Connection, settings: Settings) -> dict:
    """Tokens first, dollars second — and dollars labelled when unmeasured.

    Every `est_cost_usd` is 0 when the active model has no PRICING entry
    (`deepseek-v4-flash` is exactly this case). Tokens are always real numbers
    straight from the provider's usage object, so they are what the page
    leads with."""
    tz = ZoneInfo(settings.timezone)
    since_str, until_str = local_day_bounds_utc(settings.timezone)

    rows = conn.execute(
        "SELECT ts, provider, model, input_tokens, cached_tokens, output_tokens, est_cost_usd "
        "FROM spend_log"
    ).fetchall()

    per_day: dict[str, dict] = {}
    per_model: dict[tuple[str, str], dict] = {}
    totals = {"calls": 0, "input": 0, "cached": 0, "output": 0, "cost": 0.0}
    today = {"calls": 0, "input": 0, "cached": 0, "output": 0, "cost": 0.0}

    for row in rows:
        inp = row["input_tokens"] or 0
        cached = row["cached_tokens"] or 0
        out = row["output_tokens"] or 0
        cost = row["est_cost_usd"] or 0.0

        totals["calls"] += 1
        totals["input"] += inp
        totals["cached"] += cached
        totals["output"] += out
        totals["cost"] += cost

        if since_str <= row["ts"] < until_str:
            today["calls"] += 1
            today["input"] += inp
            today["cached"] += cached
            today["output"] += out
            today["cost"] += cost

        day = _local_day(row["ts"], tz)
        if day:
            bucket = per_day.setdefault(
                day, {"day": day, "calls": 0, "input": 0, "cached": 0, "output": 0, "cost": 0.0}
            )
            bucket["calls"] += 1
            bucket["input"] += inp
            bucket["cached"] += cached
            bucket["output"] += out
            bucket["cost"] += cost

        key = (row["provider"], row["model"])
        model_bucket = per_model.setdefault(
            key,
            {"provider": row["provider"], "model": row["model"], "calls": 0,
             "input": 0, "cached": 0, "output": 0, "cost": 0.0},
        )
        model_bucket["calls"] += 1
        model_bucket["input"] += inp
        model_bucket["cached"] += cached
        model_bucket["output"] += out
        model_bucket["cost"] += cost

    for bucket in per_model.values():
        bucket["priced"] = bucket["model"] in PRICING
        bucket["cost"] = round(bucket["cost"], 6)

    ceiling = settings.daily_token_spend_ceiling_usd
    models_priced = [bucket["priced"] for bucket in per_model.values()]

    return {
        "today": {**today, "cost": round(today["cost"], 6)},
        "totals": {**totals, "cost": round(totals["cost"], 6)},
        "ceiling_usd": ceiling,
        "ceiling_used_pct": round(today["cost"] / ceiling * 100, 1) if ceiling else None,
        # False when *no* model in the log is priced — the honest signal that
        # every dollar figure on this page is a placeholder, not a measurement.
        "cost_is_measured": any(models_priced),
        "unpriced_models": sorted(
            {bucket["model"] for bucket in per_model.values() if not bucket["priced"]}
        ),
        "cache_hit_rate": round(totals["cached"] / totals["input"] * 100, 1)
        if totals["input"]
        else None,
        "avg_input_tokens": round(totals["input"] / totals["calls"]) if totals["calls"] else None,
        "avg_output_tokens": round(totals["output"] / totals["calls"]) if totals["calls"] else None,
        "per_day": [
            per_day.get(day, {"day": day, "calls": 0, "input": 0,
                              "cached": 0, "output": 0, "cost": 0.0})
            for day in _day_axis(tz, _TIMELINE_DAYS)
        ],
        "per_model": sorted(per_model.values(), key=lambda item: -item["calls"]),
    }


def liveness(conn: sqlite3.Connection, settings: Settings) -> dict:
    """Module 27's audit trail — a delisted row vanishes from every other view.

    Coverage is the number worth staring at: only workday, greenhouse and
    lever can be verified at all, so `unverifiable` counts the shortlisted
    jobs whose posting could already be gone with no way to find out."""
    tz = ZoneInfo(settings.timezone)
    reasons = Counter()
    by_ats = Counter()
    per_day = Counter()
    recent: list[dict] = []

    for row in conn.execute(
        "SELECT company, title, ats_type, delist_reason, delisted_at, score, apply_url "
        "FROM jobs WHERE status = 'delisted' ORDER BY delisted_at DESC"
    ):
        reasons[row["delist_reason"]] += 1
        by_ats[row["ats_type"]] += 1
        day = _local_day(row["delisted_at"], tz)
        if day:
            per_day[day] += 1
        if len(recent) < _TOP_N:
            recent.append(dict(row))

    checkable = 0
    unverifiable = 0
    for row in conn.execute(
        "SELECT ats_type, score FROM jobs WHERE status = 'scored' AND score IS NOT NULL"
    ):
        if band_for(row["score"], settings) == "reject":
            continue
        if row["ats_type"] in CHECKED_ATS_TYPES:
            checkable += 1
        else:
            unverifiable += 1

    pending = conn.execute(
        f"""
        SELECT COUNT(*) AS n FROM jobs j
        LEFT JOIN job_state s ON s.global_id = j.global_id
        WHERE j.status = 'scored' AND s.global_id IS NULL
          AND j.ats_type IN ({",".join("?" * len(CHECKED_ATS_TYPES))})
        """,
        tuple(sorted(CHECKED_ATS_TYPES)),
    ).fetchone()["n"]

    last_sweep = get_daemon_state(conn, _LIVENESS_SWEEP_KEY)
    next_sweep = None
    parsed_sweep = _parse(last_sweep)
    if parsed_sweep is not None:
        next_sweep = (
            parsed_sweep + timedelta(hours=settings.liveness_sweep_interval_hours)
        ).isoformat()

    return {
        "enabled": settings.liveness_check_enabled,
        "total_delisted": sum(reasons.values()),
        "by_reason": _counter_rows(reasons),
        "by_ats": _counter_rows(by_ats),
        "per_day": _daily_series(per_day, tz),
        "recent": recent,
        "shortlisted_checkable": checkable,
        "shortlisted_unverifiable": unverifiable,
        "sweep_queue": pending,
        "last_sweep_at": last_sweep,
        "next_sweep_at": next_sweep,
        "sweep_interval_hours": settings.liveness_sweep_interval_hours,
    }


def location_health(conn: sqlite3.Connection, settings: Settings) -> dict:
    """How well the location filter is doing its job.

    `location_reason` is the audit trail a default-deny filter needs. A large
    and growing `unresolved` bucket does not mean "these were foreign" — it
    means no rule fired and config/us_cities.json has a gap, so real US jobs
    are being held back and never scored."""
    tz = ZoneInfo(settings.timezone)
    reasons = Counter()
    by_ats = Counter()
    per_day = Counter()

    for row in conn.execute(
        "SELECT location_reason, ats_type, first_seen_at FROM jobs "
        "WHERE status = 'excluded_location'"
    ):
        reasons[row["location_reason"] or "unrecorded"] += 1
        by_ats[row["ats_type"]] += 1
        day = _local_day(row["first_seen_at"], tz)
        if day:
            per_day[day] += 1

    sample = [
        dict(row)
        for row in conn.execute(
            "SELECT company, title, location, ats_type, location_reason, first_seen_at "
            "FROM jobs WHERE status = 'excluded_location' "
            "AND location_reason IN ('unresolved', 'bare_remote') "
            "ORDER BY first_seen_at DESC LIMIT ?",
            (_TOP_N,),
        )
    ]

    scored = conn.execute(
        "SELECT COUNT(*) AS n FROM jobs WHERE status = 'scored'"
    ).fetchone()["n"]
    held = sum(reasons.values())

    return {
        "total_held_back": held,
        "by_reason": _counter_rows(reasons),
        "by_ats": _counter_rows(by_ats, limit=_TOP_N),
        "per_day": _daily_series(per_day, tz),
        # The number that says "your lexicon has a hole", separated out because
        # every other reason here is a rule that fired deliberately.
        "unresolved": reasons.get("unresolved", 0) + reasons.get("bare_remote", 0),
        "held_per_scored": round(held / scored, 2) if scored else None,
        "recent_unresolved": sample,
    }


def timeline(conn: sqlite3.Connection, settings: Settings) -> dict:
    """Discovery and scoring over time, in the operator's own days."""
    tz = ZoneInfo(settings.timezone)
    discovered = Counter()
    scored = Counter()
    held = Counter()
    excluded = Counter()
    hourly_seen = Counter()
    hourly_scored = Counter()

    for row in conn.execute("SELECT status, first_seen_at, scored_at FROM jobs"):
        day = _local_day(row["first_seen_at"], tz)
        if day:
            discovered[day] += 1
            if row["status"] == JobStatus.EXCLUDED_LOCATION.value:
                held[day] += 1
            elif row["status"] == JobStatus.EXCLUDED.value:
                excluded[day] += 1
        hour = _local_hour(row["first_seen_at"], tz)
        if hour:
            hourly_seen[hour] += 1
        scored_day = _local_day(row["scored_at"], tz)
        if scored_day:
            scored[scored_day] += 1
        scored_hour = _local_hour(row["scored_at"], tz)
        if scored_hour:
            hourly_scored[scored_hour] += 1

    days = _day_axis(tz, _TIMELINE_DAYS)
    active_days = sum(1 for day in discovered.values() if day)
    return {
        "per_day": [
            {
                "day": day,
                "discovered": discovered.get(day, 0),
                "scored": scored.get(day, 0),
                "held_back": held.get(day, 0),
                "excluded": excluded.get(day, 0),
            }
            for day in days
        ],
        "per_hour": [
            {
                "hour": hour,
                "discovered": hourly_seen.get(hour, 0),
                "scored": hourly_scored.get(hour, 0),
            }
            for hour in _hour_axis(tz, _HOURLY_HOURS)
        ],
        "active_days": active_days,
        "busiest_day": max(discovered.items(), key=lambda item: item[1], default=(None, 0))[0],
        "busiest_day_count": max(discovered.values(), default=0),
    }


def digests(conn: sqlite3.Connection, settings: Settings) -> dict:
    """Every email this system has ever tried to send.

    Failures are counted separately and never advance the digest window (see
    db.last_digest_sent_at) — a failed send means those jobs are still owed to
    the next successful one, so a non-zero failure count here is the thing to
    look at, not the total."""
    tz = ZoneInfo(settings.timezone)
    rows = [
        dict(row)
        for row in conn.execute(
            "SELECT sent_at, job_count, status, error FROM email_log ORDER BY sent_at DESC"
        )
    ]
    sent = [row for row in rows if row["status"] == "sent"]
    since_str, until_str = local_day_bounds_utc(settings.timezone)
    sent_today = [row for row in sent if since_str <= row["sent_at"] < until_str]

    per_day = Counter()
    for row in sent:
        day = _local_day(row["sent_at"], tz)
        if day:
            per_day[day] += row["job_count"]

    return {
        "total": len(rows),
        "sent": len(sent),
        "failed": len(rows) - len(sent),
        "jobs_emailed": sum(row["job_count"] for row in sent),
        "avg_jobs_per_digest": round(sum(row["job_count"] for row in sent) / len(sent), 1)
        if sent
        else None,
        "last_sent_at": sent[0]["sent_at"] if sent else None,
        "last_job_count": sent[0]["job_count"] if sent else None,
        "sent_today": len(sent_today),
        "scheduled_at": settings.digest_time_pdt,
        "recent": rows[:_RECENT_DIGESTS],
        "per_day": _daily_series(per_day, tz),
    }


def errors_report(conn: sqlite3.Connection) -> dict:
    by_stage = Counter()
    by_type = Counter()
    unresolved = 0
    total = 0
    for row in conn.execute("SELECT stage, error_type, resolved FROM errors"):
        total += 1
        by_stage[row["stage"]] += 1
        by_type[row["error_type"] or "—"] += 1
        if not row["resolved"]:
            unresolved += 1

    recent = [
        dict(row)
        for row in conn.execute(
            "SELECT id, ts, stage, source_file, function_name, provider, job_ref, "
            "error_type, error_message, retry_count, resolved "
            "FROM errors ORDER BY ts DESC LIMIT ?",
            (_RECENT_ERRORS,),
        )
    ]
    return {
        "total": total,
        "unresolved": unresolved,
        "by_stage": _counter_rows(by_stage),
        "by_type": _counter_rows(by_type),
        "recent": recent,
    }


def companies(conn: sqlite3.Connection, settings: Settings) -> dict:
    """Who is actually hiring you, by shortlisted count rather than by volume.

    Ranked on shortlisted rather than total rows on purpose: a source that
    dumps 400 rejects is not a company you care about."""
    stats: dict[str, dict] = {}
    for row in conn.execute(
        "SELECT company, score, ats_type FROM jobs WHERE status = 'scored' AND score IS NOT NULL"
    ):
        entry = stats.setdefault(
            row["company"],
            {"company": row["company"], "scored": 0, "shortlisted": 0, "strong": 0,
             "sum": 0, "best": 0, "ats_type": row["ats_type"]},
        )
        band = band_for(row["score"], settings)
        entry["scored"] += 1
        entry["sum"] += row["score"]
        entry["best"] = max(entry["best"], row["score"])
        if band != "reject":
            entry["shortlisted"] += 1
        if band == "strong":
            entry["strong"] += 1

    ranked = sorted(
        (
            {**entry, "avg": round(entry["sum"] / entry["scored"], 1)}
            for entry in stats.values()
        ),
        key=lambda item: (-item["shortlisted"], -item["best"]),
    )
    return {
        "distinct": len(stats),
        "with_shortlist": sum(1 for entry in stats.values() if entry["shortlisted"]),
        "top": ranked[:_TOP_N],
    }


def decisions(conn: sqlite3.Connection, settings: Settings) -> dict:
    """What you did with what the pipeline handed you."""
    tz = ZoneInfo(settings.timezone)
    marks = {
        row["global_id"]: (row["state"], row["updated_at"])
        for row in conn.execute("SELECT global_id, state, updated_at FROM job_state")
    }
    applied = declined = 0
    per_day = Counter()
    for state, updated_at in marks.values():
        if state == "applied":
            applied += 1
            day = _local_day(updated_at, tz)
            if day:
                per_day[day] += 1
        elif state == "declined":
            declined += 1

    shortlisted = sum(
        1
        for row in conn.execute(
            "SELECT score FROM jobs WHERE status = 'scored' AND score IS NOT NULL"
        )
        if band_for(row["score"], settings) != "reject"
    )
    return {
        "applied": applied,
        "declined": declined,
        "shortlisted": shortlisted,
        "untouched": max(shortlisted - applied - declined, 0),
        "applied_rate": round(applied / shortlisted * 100, 1) if shortlisted else None,
        "per_day": _daily_series(per_day, tz),
    }


def analytics(conn: sqlite3.Connection, settings: Settings) -> dict:
    """Everything, in one request.

    One payload rather than a dozen endpoints: this page is read whole, the
    numbers must all describe the same instant (a spend figure from a second
    before a scored count is a bug report waiting to happen), and every query
    here shares the same read-only connection and snapshot."""
    return {
        "generated_at": datetime.now(UTC).isoformat(),
        "config": config_summary(settings),
        "live": live(conn, settings),
        "database": database(conn, settings),
        "funnel": funnel(conn, settings),
        "sources": sources(conn, settings),
        "scoring": scoring(conn, settings),
        "spend": spend(conn, settings),
        "liveness": liveness(conn, settings),
        "location": location_health(conn, settings),
        "timeline": timeline(conn, settings),
        "digests": digests(conn, settings),
        "errors": errors_report(conn),
        "companies": companies(conn, settings),
        "decisions": decisions(conn, settings),
    }
