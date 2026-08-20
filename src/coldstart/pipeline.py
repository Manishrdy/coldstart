from __future__ import annotations

import gc
import sqlite3
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
from pydantic import BaseModel

from coldstart.budget import BudgetExceeded, today_spend
from coldstart.db import (
    connection,
    count_unresolved_errors,
    get_digest_jobs,
    init_schema,
    last_digest_sent_at,
    load_seen_keys,
    log_run,
    providers_used_since,
    run_totals_since,
    set_slice_state,
    upsert_job,
)
from coldstart.dedupe import dedupe
from coldstart.digest import build_digest_html, build_digest_sections, send_digest
from coldstart.errors import log_error
from coldstart.export import export_csv
from coldstart.fetcher import RAWJOB_COLUMNS, download_slice, load_slice
from coldstart.filters.company import filter_companies
from coldstart.filters.eligibility import filter_eligibility
from coldstart.filters.freshness import filter_freshness
from coldstart.filters.location import filter_locations
from coldstart.filters.title import filter_titles
from coldstart.logging_setup import get_logger, new_run_id
from coldstart.manifest_watch import (
    SliceInfo,
    changed_slices,
    fetch_manifest,
    load_excluded_ats,
    relevant_slices,
)
from coldstart.models import (
    EligibilityFlag,
    JobRecord,
    JobScore,
    JobStatus,
    LocationFlag,
    RawJob,
    ResumeId,
    SliceState,
)
from coldstart.resume_ingest import check_resumes_ready, ingest_resumes, load_existing_slots
from coldstart.routing import ResumeManifest, load_resume_manifest, route
from coldstart.scoring.base import LLMProvider
from coldstart.scoring.providers import build_active_provider
from coldstart.scoring.scorer import score_job
from coldstart.settings import Settings

logger = get_logger(__name__)

# Same repo-relative resolution as filters/_shared.py's _CONFIG_DIR — there's
# no settings field for this path, consistent with title_rules.json/
# us_states.json etc. being fixed config, not per-deployment configuration.
_EXCLUDED_ATS_PATH = Path(__file__).resolve().parent.parent.parent / "config" / "excluded_ats.json"


class PollResult(BaseModel):
    run_id: str
    slices_processed: int
    fetched_count: int
    filtered_count: int
    scored_count: int
    failed_count: int
    excluded_count: int
    csv_path: str | None


class _SliceStats(BaseModel):
    fetched: int
    filtered: int
    scored: int
    failed: int
    excluded: int
    records: list[JobRecord]


def _local_date(tz: str) -> date:
    return datetime.now(UTC).astimezone(ZoneInfo(tz)).date()


def _clean(value: object) -> object:
    # pandas represents missing values as NaN (a float), not None — this bit
    # the project before (see DEVELOPMENT_PLAN.md's real-data findings).
    if value is None:
        return None
    if isinstance(value, float) and pd.isna(value):
        return None
    return value


def _clean_str(value: object) -> str | None:
    # Real-data finding (DEVELOPMENT_PLAN.md Module 19): several ATS feeds
    # (amazon, cornerstone, dayforce, paylocity, ...) store requisition_id
    # as a bare int/float rather than a string, which pydantic's `str`
    # field rejects outright rather than coercing — RawJob construction
    # crashed the entire slice on the first such row. Any RawJob field
    # that's semantically a string but sourced from a loosely-typed parquet
    # column goes through this instead of _clean.
    value = _clean(value)
    if value is None:
        return None
    if isinstance(value, float) and value.is_integer():
        return str(int(value))  # avoid a spurious "10492887.0"
    return str(value)


def _row_to_raw_job(row) -> RawJob:
    raw = _clean(row.raw)
    return RawJob(
        global_id=row.global_id,
        requisition_id=_clean_str(row.requisition_id),
        company=row.company,
        title=row.title,
        location=_clean_str(row.location),
        country_iso=_clean_str(getattr(row, "country_iso", None)),
        is_remote=_clean(getattr(row, "is_remote", None)),
        apply_url=_clean_str(row.apply_url),
        url=row.url,
        ats_type=row.ats_type,
        posted_at=_clean(row.posted_at),
        description=_clean_str(row.description),
        experience=_clean(getattr(row, "experience", None)),
        raw=raw if isinstance(raw, dict) else None,
    )


def _excluded_record(row) -> JobRecord:
    raw_job = _row_to_raw_job(row)
    return JobRecord(
        global_id=raw_job.global_id,
        requisition_id=raw_job.requisition_id,
        company=raw_job.company,
        title=raw_job.title,
        location=raw_job.location,
        apply_url=raw_job.apply_url,
        ats_type=raw_job.ats_type,
        posted_at=raw_job.posted_at,
        status=JobStatus.EXCLUDED,
        location_flag=LocationFlag(row.location_flag),
        eligibility_flag=EligibilityFlag.EXCLUDED,
        first_seen_at=datetime.now(UTC),
    )


def _to_record(
    provider: LLMProvider,
    job: RawJob,
    score: JobScore | None,
    resume_id: ResumeId,
    location_flag: LocationFlag,
    eligibility_flag: EligibilityFlag,
) -> JobRecord:
    now = datetime.now(UTC)
    common = dict(
        global_id=job.global_id,
        requisition_id=job.requisition_id,
        company=job.company,
        title=job.title,
        location=job.location,
        apply_url=job.apply_url,
        ats_type=job.ats_type,
        posted_at=job.posted_at,
        resume_used=resume_id,
        location_flag=location_flag,
        eligibility_flag=eligibility_flag,
        first_seen_at=now,
    )

    if score is None:
        return JobRecord(**common, status=JobStatus.FAILED)

    return JobRecord(
        **common,
        score=score.score,
        score_band=score.score_band,
        eligible=score.eligible,
        matched_skills=score.matched_skills,
        missing_skills=score.missing_skills,
        reasoning=score.reasoning,
        status=JobStatus.SCORED,
        provider_used=provider.name,
        scored_at=now,
    )


def _process_slice(
    conn: sqlite3.Connection,
    slice_info: SliceInfo,
    settings: Settings,
    provider: LLMProvider,
    resume_manifest: ResumeManifest,
    resume_texts: dict[ResumeId, str],
    seen_global: set[str],
    seen_req: set[tuple[str, str, str]],
) -> _SliceStats:
    path = download_slice(slice_info, settings.data_dir, conn)
    df = load_slice(path, columns=RAWJOB_COLUMNS)
    fetched = len(df)

    df = filter_titles(df)
    # Runs before location/eligibility/routing/scoring on purpose: a blocked
    # company must never reach an LLM, and everything after this point either
    # costs money or persists a row. See filters/company.py.
    df = filter_companies(df)
    # Before location/eligibility because it's a date compare and drops ~95%
    # of what survives the title filter — cheapest, most decisive first.
    df = filter_freshness(df, settings.max_posting_age_days)
    df = filter_locations(df)
    df = df[df["location_flag"] != LocationFlag.REJECTED.value].reset_index(drop=True)
    df = filter_eligibility(df)

    excluded_mask = df["eligibility_flag"] == EligibilityFlag.EXCLUDED.value
    excluded_records = [_excluded_record(row) for row in df[excluded_mask].itertuples()]
    for record in excluded_records:
        upsert_job(conn, record)

    df = df[~excluded_mask].reset_index(drop=True)
    filtered = len(df)

    df = dedupe(df, seen_global, seen_req)

    records: list[JobRecord] = list(excluded_records)
    scored_count = 0
    failed_count = 0

    for row in df.itertuples():
        raw_job = _row_to_raw_job(row)
        resume_id, _method = route(raw_job, resume_manifest, provider)
        score = score_job(raw_job, resume_texts[resume_id], provider, conn, settings)
        record = _to_record(
            provider,
            raw_job,
            score,
            resume_id,
            LocationFlag(row.location_flag),
            EligibilityFlag(row.eligibility_flag),
        )
        upsert_job(conn, record)
        records.append(record)

        if score is not None:
            scored_count += 1
        else:
            failed_count += 1

        seen_global.add(raw_job.global_id)
        if raw_job.requisition_id:
            seen_req.add((raw_job.company, raw_job.requisition_id, raw_job.location))

    set_slice_state(
        conn,
        SliceState(
            ats_type=slice_info.ats_type,
            last_sha256=slice_info.sha256,
            last_processed_at=datetime.now(UTC),
            row_count=slice_info.rows,
        ),
    )

    stats = _SliceStats(
        fetched=fetched,
        filtered=filtered,
        scored=scored_count,
        failed=failed_count,
        excluded=len(excluded_records),
        records=records,
    )
    del df
    gc.collect()
    return stats


def run_poll(settings: Settings) -> PollResult:
    run_id = new_run_id()
    started_at = datetime.now(UTC)

    with connection(settings.db_path) as conn:
        init_schema(conn)

        resumes_dir = settings.resume_manifest.parent
        provider = build_active_provider(settings)

        if check_resumes_ready(resumes_dir, settings.resume_manifest):
            resolved_resumes = load_existing_slots(resumes_dir)
        else:
            # Raises ResumesNotReady (already logged CRITICAL + to `errors`
            # inside resume_ingest.py) — left uncaught here on purpose, so it
            # aborts the whole run before any network/LLM spend.
            resolved_resumes = ingest_resumes(
                resumes_dir, settings.resume_manifest, provider, conn
            )
        resume_texts = {slot: record.full_text for slot, record in resolved_resumes.items()}
        resume_manifest = load_resume_manifest(settings.resume_manifest)

        excluded = load_excluded_ats(_EXCLUDED_ATS_PATH)
        manifest = fetch_manifest(settings.manifest_url)
        candidate_slices = relevant_slices(manifest, excluded, conn)
        slices = changed_slices(conn, candidate_slices)

        if not slices:
            logger.info("no changed slices — nothing to do")
            return PollResult(
                run_id=run_id,
                slices_processed=0,
                fetched_count=0,
                filtered_count=0,
                scored_count=0,
                failed_count=0,
                excluded_count=0,
                csv_path=None,
            )

        seen_global, seen_req = load_seen_keys(conn)

        slices_processed = 0
        fetched_total = filtered_total = scored_total = failed_total = excluded_total = 0
        all_records: list[JobRecord] = []

        for slice_info in slices:
            try:
                stats = _process_slice(
                    conn,
                    slice_info,
                    settings,
                    provider,
                    resume_manifest,
                    resume_texts,
                    seen_global,
                    seen_req,
                )
            except BudgetExceeded:
                # The circuit breaker must halt the whole run, not just this
                # slice — deliberately NOT caught-and-continued like other
                # per-slice failures below. Jobs already scored this run are
                # already committed (per-job upsert_job), so nothing is lost;
                # only this run's CSV export (below) doesn't happen.
                raise
            except Exception as exc:
                log_error(
                    conn,
                    stage="poll",
                    exc=exc,
                    source_file=__name__,
                    function_name="run_poll",
                    job_ref=slice_info.ats_type,
                )
                logger.error(
                    "slice %s failed, continuing with remaining slices: %s",
                    slice_info.ats_type,
                    exc,
                )
                continue

            slices_processed += 1
            fetched_total += stats.fetched
            filtered_total += stats.filtered
            scored_total += stats.scored
            failed_total += stats.failed
            excluded_total += stats.excluded
            all_records.extend(stats.records)

        csv_path: str | None = None
        if all_records:
            run_date = _local_date(settings.timezone)
            csv_path = str(export_csv(all_records, settings.output_dir, run_date))

        log_run(
            conn,
            run_id=run_id,
            started_at=started_at,
            finished_at=datetime.now(UTC),
            fetched_count=fetched_total,
            filtered_count=filtered_total,
            scored_count=scored_total,
            failed_count=failed_total,
        )

        logger.info(
            "run_poll done: run_id=%s slices=%d fetched=%d filtered=%d scored=%d "
            "failed=%d excluded=%d",
            run_id,
            slices_processed,
            fetched_total,
            filtered_total,
            scored_total,
            failed_total,
            excluded_total,
        )

        return PollResult(
            run_id=run_id,
            slices_processed=slices_processed,
            fetched_count=fetched_total,
            filtered_count=filtered_total,
            scored_count=scored_total,
            failed_count=failed_total,
            excluded_count=excluded_total,
            csv_path=csv_path,
        )


# Used only when no digest has ever been sent. A full day is a sensible
# first window: wide enough to include an evening's work on the run that
# prompted the first digest, narrow enough not to dump an entire backlog.
_FIRST_DIGEST_LOOKBACK_HOURS = 24

# A window wider than this means digests stopped going out for a while. Not an
# error — the jobs genuinely haven't been reported yet and still should be —
# but worth saying out loud, since the email will be unusually large.
_WIDE_WINDOW_WARNING_DAYS = 3


def _digest_window_start(conn: sqlite3.Connection, settings: Settings) -> datetime:
    """Cover everything since the last digest actually went out.

    Previously this was local midnight, which quietly meant the 08:00 digest
    reported only the overnight hours — anything found between 08:00 and
    midnight was saved, shown on the dashboard, and never emailed. Anchoring
    to the last successful send makes coverage continuous by construction: no
    matter what hour a job is found, some digest's window contains it."""
    last_sent = last_digest_sent_at(conn)
    now = datetime.now(UTC)

    if last_sent is None:
        since = now - timedelta(hours=_FIRST_DIGEST_LOOKBACK_HOURS)
        logger.info(
            "no digest has been sent before — covering the last %d hours",
            _FIRST_DIGEST_LOOKBACK_HOURS,
        )
        return since

    span_days = (now - last_sent).total_seconds() / 86400
    if span_days > _WIDE_WINDOW_WARNING_DAYS:
        logger.warning(
            "last digest was %.1f days ago — this one covers that whole gap and may be large",
            span_days,
        )
    logger.info("digest covers everything since the last one, sent %s", last_sent.isoformat())
    return last_sent


def run_digest(settings: Settings) -> bool:
    with connection(settings.db_path) as conn:
        init_schema(conn)

        today = _local_date(settings.timezone)
        since = _digest_window_start(conn, settings)

        jobs = get_digest_jobs(conn, since=since)
        totals = run_totals_since(conn, since)
        csv_path = settings.output_dir / f"scored_{today.isoformat()}.csv"

        sections = build_digest_sections(
            jobs,
            settings,
            fetched_count=totals["fetched"],
            filtered_count=totals["filtered"],
            scored_count=totals["scored"],
            failed_count=totals["failed"],
            # Spend stays a *daily* number on purpose — it's measured
            # against DAILY_TOKEN_SPEND_CEILING_USD, which is a per-day
            # concept regardless of what window this digest covers.
            spend_today_usd=today_spend(conn, settings.timezone),
            providers_used=providers_used_since(conn, since),
            csv_path=csv_path,
            unresolved_errors_count=count_unresolved_errors(conn),
            window_start=since,
        )
        html_body = build_digest_html(sections, today)

        # Sections aren't mutually exclusive (a job can be both "strong" and
        # "location uncertain" — see digest.py), so email_log.job_count is a
        # distinct count of jobs shown, not a sum across sections.
        shown = (
            sections.strong
            + sections.consider
            + sections.location_uncertain
            + sections.eligibility_uncertain
        )
        job_count = len({job.global_id for job in shown})
        subject = (
            f"Coldstart Digest — {today.isoformat()} — {len(sections.strong)} strong match(es)"
        )

        return send_digest(settings, html_body, subject, job_count, conn)
