from __future__ import annotations

import gc
import os
import re
import sqlite3
import time
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
from pydantic import BaseModel

from coldstart.ats_control import (
    max_yields_for,
    order_slices,
    read_control,
    reason_to_yield,
)
from coldstart.budget import BudgetExceeded, local_day_bounds_utc, today_spend
from coldstart.db import (
    connection,
    count_unresolved_errors,
    digest_sent_today,
    get_digest_jobs,
    get_slice_state,
    init_schema,
    last_digest_sent_at,
    load_seen_keys,
    log_run,
    mark_delisted,
    providers_used_since,
    run_totals_since,
    scored_jobs_for_liveness_check,
    set_slice_state,
    upsert_job,
)
from coldstart.dedupe import dedupe
from coldstart.digest import (
    build_digest_html,
    build_digest_sections,
    render_digest_text,
    send_digest,
)
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
    LivenessFlag,
    LocationFlag,
    RawJob,
    ResumeId,
    SliceState,
)
from coldstart.progress import HEARTBEAT_SECONDS, PollProgress, write_progress
from coldstart.resume_ingest import check_resumes_ready, ingest_resumes, load_existing_slots
from coldstart.routing import ResumeManifest, load_resume_manifest, route
from coldstart.scoring.base import LLMProvider
from coldstart.scoring.providers import build_active_provider
from coldstart.scoring.scorer import score_job
from coldstart.settings import Settings
from coldstart.verify import CHECKED_ATS_TYPES, check_still_live

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
    location_excluded_count: int
    delisted_count: int
    stale_by_live_data_count: int
    # Sources that gave way to a higher-priority one and went back in the
    # queue. They are still outstanding — not failures, not skips.
    preempted: list[str] = []
    # Sources the operator is holding. Deliberately untouched this run.
    held: list[str] = []
    csv_path: str | None


class _SliceStats(BaseModel):
    fetched: int
    filtered: int
    scored: int
    failed: int
    excluded: int
    location_excluded: int
    delisted: int
    stale_by_live_data: int
    records: list[JobRecord]
    # Set when the operator reordered the queue mid-slice (Module 29).
    # The slice stopped where it was and its slice_state was NOT written,
    # so it is still outstanding and run_poll puts it back in the queue.
    yielded_to: str | None = None

    @property
    def preempted(self) -> bool:
        return self.yielded_to is not None


class LivenessSweepResult(BaseModel):
    checked_count: int
    delisted_count: int


class _ProgressReporter:
    """Owns this run's PollProgress row and decides when to write it.

    Module 28. The analytics page needs to answer "what is it doing right
    now", and run_poll is a subprocess with no way to reach the daemon's
    in-memory state — see progress.py for why `daemon_state` is the channel.

    Phase changes are written immediately because they are rare and are
    exactly the transitions worth seeing; per-job progress is throttled to
    HEARTBEAT_SECONDS, since a slice can walk hundreds of jobs and each write
    is a commit on the same connection the poll uses for real work."""

    def __init__(
        self, conn: sqlite3.Connection, run_id: str, started_at: datetime
    ) -> None:
        self._conn = conn
        # monotonic, not wall clock: a clock step (NTP, DST) must not stall
        # the heartbeat for hours or turn it into a write per job.
        self._last_write = 0.0
        self.progress = PollProgress(
            run_id=run_id,
            pid=os.getpid(),
            started_at=started_at,
            updated_at=started_at,
            phase="preparing",
        )
        self.flush(force=True)

    def flush(self, *, force: bool = False) -> None:
        now = time.monotonic()
        if not force and now - self._last_write < HEARTBEAT_SECONDS:
            return
        self._last_write = now
        self.progress.updated_at = datetime.now(UTC)
        write_progress(self._conn, self.progress)

    def phase(self, name: str, **fields: int) -> None:
        for key, value in fields.items():
            setattr(self.progress, key, value)
        self.progress.phase = name
        self.flush(force=True)

    def slice_started(self, index: int, slice_info: SliceInfo) -> None:
        self.progress.slice_index = index
        self.progress.ats_type = slice_info.ats_type
        self.progress.slice_rows = slice_info.rows
        self.progress.slice_fetched = 0
        self.progress.slice_candidates = 0
        self.progress.slice_processed = 0
        self.phase("downloading")

    def bump(self, **counts: int) -> None:
        """Add to the run-so-far totals. Throttled — this is the hot path."""
        for key, value in counts.items():
            setattr(self.progress, key, getattr(self.progress, key) + value)
        self.flush()

    def finish(self) -> None:
        """Stamp the run as over.

        A reader treats a heartbeat with no `finished_at` and a dead pid as a
        run that was killed, which is exactly right — so this must run on the
        way out of every path the process actually survives, including a
        budget halt or an unhandled error."""
        self.progress.finished_at = datetime.now(UTC)
        self.progress.phase = "done"
        self.progress.ats_type = None
        self.flush(force=True)


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


# An `apply_url` that leads to a sign-in form instead of the job. Every one
# of ycombinator's rows carries
# `account.ycombinator.com/authenticate?continue=...workatastartup.com/...`,
# which renders a YC signup page showing nothing about the posting — you
# cannot read the job, let alone apply, without a YC account. The public
# listing at `ycombinator.com/companies/<co>/jobs/<id>` lives in `url`
# (scope.md §3.1.1) and is the link worth clicking.
#
# Deliberately matched by exact host+path, not by a generic "looks like a
# login" heuristic: plenty of legitimate `apply_url`s contain the words
# "login" or "signup" as query noise while still opening the posting.
_LOGIN_WALLED_APPLY_RE = re.compile(
    r"^https?://account\.ycombinator\.com/authenticate\b", re.IGNORECASE
)


def _best_link(job: RawJob) -> str | None:
    """The link to put in front of the operator.

    `apply_url` is the dedicated application link, but it is genuinely absent
    on some sources — every one of amazon's 33,888 rows has it as NaN, which
    is why a digest built from that slice showed an empty Apply column for
    every single job. `url` (the posting page) is a required field and is
    always populated, so it is the fallback. A link to the posting is far more
    useful than no link at all.

    A *present* `apply_url` can be just as useless: a login wall is a link
    that goes somewhere and tells you nothing (2026-08-25, found by clicking
    a YC job on the dashboard). Same failure as the empty amazon column, so
    it takes the same fallback rather than a second mechanism.

    verify.py liveness-checks `jobs.apply_url` for workday/greenhouse/lever
    only (`CHECKED_ATS_TYPES`); none of those are walled, so what it reads is
    unchanged. If a walled source is ever added there, the real apply link
    has to be persisted separately — do not widen this pattern to cover it."""
    apply_url = job.apply_url
    if apply_url and _LOGIN_WALLED_APPLY_RE.match(apply_url):
        return job.url or apply_url
    return apply_url or job.url or None


def _excluded_record(
    row,
    *,
    status: JobStatus = JobStatus.EXCLUDED,
    eligibility_flag: EligibilityFlag = EligibilityFlag.EXCLUDED,
) -> JobRecord:
    raw_job = _row_to_raw_job(row)
    return JobRecord(
        global_id=raw_job.global_id,
        requisition_id=raw_job.requisition_id,
        company=raw_job.company,
        title=raw_job.title,
        location=raw_job.location,
        apply_url=_best_link(raw_job),
        ats_type=raw_job.ats_type,
        posted_at=raw_job.posted_at,
        status=status,
        location_flag=LocationFlag(row.location_flag),
        location_reason=getattr(row, "location_reason", None),
        eligibility_flag=eligibility_flag,
        first_seen_at=datetime.now(UTC),
    )


def _delisted_record(row, reason: str) -> JobRecord:
    """A row that survived every filter but failed the pre-LLM liveness
    check — confirmed gone at the source ATS before a cent was spent scoring
    it. Same shape as _excluded_record, persisted the same way, but scoring
    genuinely never ran, so eligibility_flag is honestly UNCERTAIN rather
    than EXCLUDED (that value means the eligibility filter actively rejected
    it, which didn't happen here)."""
    raw_job = _row_to_raw_job(row)
    now = datetime.now(UTC)
    return JobRecord(
        global_id=raw_job.global_id,
        requisition_id=raw_job.requisition_id,
        company=raw_job.company,
        title=raw_job.title,
        location=raw_job.location,
        apply_url=_best_link(raw_job),
        ats_type=raw_job.ats_type,
        posted_at=raw_job.posted_at,
        status=JobStatus.DELISTED,
        location_flag=LocationFlag(row.location_flag),
        location_reason=getattr(row, "location_reason", None),
        eligibility_flag=EligibilityFlag.UNCERTAIN,
        first_seen_at=now,
        delist_reason=reason,
        delisted_at=now,
    )


def _to_record(
    provider: LLMProvider,
    job: RawJob,
    score: JobScore | None,
    resume_id: ResumeId,
    location_flag: LocationFlag,
    eligibility_flag: EligibilityFlag,
    location_reason: str | None = None,
) -> JobRecord:
    now = datetime.now(UTC)
    common = dict(
        global_id=job.global_id,
        requisition_id=job.requisition_id,
        company=job.company,
        title=job.title,
        location=job.location,
        apply_url=_best_link(job),
        ats_type=job.ats_type,
        posted_at=job.posted_at,
        resume_used=resume_id,
        location_flag=location_flag,
        location_reason=location_reason,
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


def _yielded(reason: str, *, fetched: int = 0) -> _SliceStats:
    """A slice that gave way before it committed anything.

    `fetched` is still reported when the parquet had already been read —
    that work genuinely happened, and run_log measures work performed by a
    run, not distinct postings."""
    return _SliceStats(
        fetched=fetched, filtered=0, scored=0, failed=0, excluded=0,
        location_excluded=0, delisted=0, stale_by_live_data=0,
        records=[], yielded_to=reason,
    )


def _make_yield_check(
    conn: sqlite3.Connection,
    current: str,
    candidates: list[SliceInfo],
    failed: set[str],
):
    """Ask the database — not a cached copy — whether to give way.

    Both halves have to be live. The operator clicks while the slice is
    already running, so a control value read at slice start would never see
    it; and the source they promote is often one this run never queued
    (because it was up to date when the run began), so a `pending` set frozen
    at slice start would not contain it either. That second case is the
    normal one, and getting it wrong makes the whole control silently do
    nothing.

    Cheap despite sitting between LLM calls: only sources named ahead of
    `current` in the priority list are ever looked up — usually none, at most
    one — and each lookup is a single primary-key read."""
    by_ats = {item.ats_type: item for item in candidates}

    def outstanding_ahead(control) -> set[str]:
        pending: set[str] = set()
        for name in control.priority:
            if name == current:
                break                      # nothing after us can outrank us
            candidate = by_ats.get(name)
            if candidate is None or name in failed:
                continue
            state = get_slice_state(conn, name)
            if state is None or state.last_sha256 != candidate.sha256:
                pending.add(name)
        return pending

    def check() -> str | None:
        control = read_control(conn)
        return reason_to_yield(control, current, outstanding_ahead(control))

    return check


def _never_yield() -> None:
    return None


def _process_slice(
    conn: sqlite3.Connection,
    slice_info: SliceInfo,
    settings: Settings,
    provider: LLMProvider,
    resume_manifest: ResumeManifest,
    resume_texts: dict[ResumeId, str],
    seen_global: set[str],
    seen_req: set[tuple[str, str, str]],
    reporter: _ProgressReporter,
    should_yield=_never_yield,
) -> _SliceStats:
    # Before the download, because that is the one step that cannot be
    # interrupted cleanly — it streams to a `.part` file and renames on
    # success, so abandoning it halfway throws the whole transfer away.
    if (reason := should_yield()) is not None:
        reporter.phase("yielding")
        return _yielded(reason)

    path = download_slice(slice_info, settings.data_dir, conn)
    reporter.phase("reading")
    df = load_slice(path, columns=RAWJOB_COLUMNS)
    fetched = len(df)
    reporter.phase("filtering", slice_fetched=fetched)

    # Straight after the expensive read and before the filters commit
    # anything — the next chance to stop is otherwise the scoring loop,
    # several minutes of pandas away on a slice this size.
    if (reason := should_yield()) is not None:
        reporter.phase("yielding")
        return _yielded(reason, fetched=fetched)

    df = filter_titles(df)
    # Runs before location/eligibility/routing/scoring on purpose: a blocked
    # company must never reach an LLM, and everything after this point either
    # costs money or persists a row. See filters/company.py.
    df = filter_companies(df)
    # Before location/eligibility because it's a date compare and drops ~95%
    # of what survives the title filter — cheapest, most decisive first.
    df = filter_freshness(df, settings.max_posting_age_days)
    df = filter_locations(df)
    # REJECTED is dropped without a trace: the volume is enormous and a
    # hard-rejected row tells you nothing worth reviewing.
    df = df[df["location_flag"] != LocationFlag.REJECTED.value].reset_index(drop=True)

    # Default-deny UNCERTAIN. It used to flow straight to the LLM, which meant
    # paying a provider to read a job in Bengaluru — 42% of scored rows in one
    # run were location-uncertain. These are persisted rather than dropped so
    # the filter stays auditable: location_reason names the rule that fired,
    # or says "unresolved"/"bare_remote", meaning no rule fired and the
    # lexicon has a gap. The dashboard's held-back view is where that shows.
    uncertain_mask = df["location_flag"] == LocationFlag.UNCERTAIN.value
    location_excluded_records = [
        _excluded_record(
            row,
            status=JobStatus.EXCLUDED_LOCATION,
            # Eligibility never ran on these rows — they stopped a filter
            # earlier — so UNCERTAIN is the honest value, not EXCLUDED.
            eligibility_flag=EligibilityFlag.UNCERTAIN,
        )
        for row in df[uncertain_mask].itertuples()
    ]
    for record in location_excluded_records:
        upsert_job(conn, record)
    reporter.bump(location_excluded=len(location_excluded_records))
    df = df[~uncertain_mask].reset_index(drop=True)

    df = filter_eligibility(df)

    excluded_mask = df["eligibility_flag"] == EligibilityFlag.EXCLUDED.value
    excluded_records = [_excluded_record(row) for row in df[excluded_mask].itertuples()]
    for record in excluded_records:
        upsert_job(conn, record)
    reporter.bump(excluded=len(excluded_records))

    df = df[~excluded_mask].reset_index(drop=True)
    filtered = len(df)

    df = dedupe(df, seen_global, seen_req)
    reporter.phase("scoring", slice_candidates=len(df))
    yielded_to: str | None = None

    records: list[JobRecord] = [*location_excluded_records, *excluded_records]
    scored_count = 0
    failed_count = 0
    delisted_count = 0
    stale_by_live_data_count = 0

    for processed, row in enumerate(df.itertuples(), start=1):
        # Between jobs, never mid-job: a job is upserted as one unit, and
        # stopping after the LLM call but before the write would spend the
        # money and throw the answer away.
        if (yielded_to := should_yield()) is not None:
            logger.info(
                "%s giving way (%s) after %d of %d job(s) — it stays outstanding",
                slice_info.ats_type, yielded_to, processed - 1, len(df),
            )
            reporter.phase("yielding")
            break

        reporter.progress.slice_processed = processed
        raw_job = _row_to_raw_job(row)

        # Cheaper filters already ran; this is the last, most expensive gate
        # before an LLM call, and the only one that costs a network request —
        # exactly why it runs last, on the smallest possible surviving set.
        # scope.md §3.3: the snapshot this row came from can already be stale
        # by the time it's downloaded, so a posting can be dead on arrival.
        if settings.liveness_check_enabled and raw_job.ats_type in CHECKED_ATS_TYPES:
            check = check_still_live(raw_job.ats_type, _best_link(raw_job), raw_job.company)

            if check.flag is LivenessFlag.DEAD:
                record = _delisted_record(row, check.reason)
                upsert_job(conn, record)
                records.append(record)
                delisted_count += 1
                reporter.bump(delisted=1)

                seen_global.add(raw_job.global_id)
                if raw_job.requisition_id:
                    seen_req.add((raw_job.company, raw_job.requisition_id, raw_job.location))
                continue

            # Bonus signal from the same CXS call (verify.py): Workday's own
            # "Posted N Days Ago" is more accurate than the snapshot's
            # posted_at, which filter_freshness already ran against upstream
            # of here. Same treatment as that filter — dropped, not
            # persisted, since it's the identical fact, just from a better
            # source. Still marked seen: re-confirming it costs a real
            # request against Workday, and the answer won't change.
            if (
                check.flag is LivenessFlag.LIVE
                and check.posted_days_ago is not None
                and check.posted_days_ago > settings.max_posting_age_days
            ):
                stale_by_live_data_count += 1
                reporter.flush()
                seen_global.add(raw_job.global_id)
                if raw_job.requisition_id:
                    seen_req.add((raw_job.company, raw_job.requisition_id, raw_job.location))
                continue

        resume_id, _method = route(raw_job, resume_manifest, provider)
        score = score_job(raw_job, resume_texts[resume_id], provider, conn, settings)
        record = _to_record(
            provider,
            raw_job,
            score,
            resume_id,
            LocationFlag(row.location_flag),
            EligibilityFlag(row.eligibility_flag),
            location_reason=getattr(row, "location_reason", None),
        )
        upsert_job(conn, record)
        records.append(record)

        if score is not None:
            scored_count += 1
            reporter.bump(scored=1)
        else:
            failed_count += 1
            reporter.bump(failed=1)

        seen_global.add(raw_job.global_id)
        if raw_job.requisition_id:
            seen_req.add((raw_job.company, raw_job.requisition_id, raw_job.location))

    # NOT written when the slice gave way. This single line is what makes a
    # yield safe: without a slice_state row matching the current sha256,
    # changed_slices still reports the source as outstanding and it comes
    # back round — this run, or the next one.
    if yielded_to is None:
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
        location_excluded=len(location_excluded_records),
        delisted=delisted_count,
        stale_by_live_data=stale_by_live_data_count,
        records=records,
        yielded_to=yielded_to,
    )
    del df
    gc.collect()
    return stats


def run_poll(settings: Settings) -> PollResult:
    run_id = new_run_id()
    started_at = datetime.now(UTC)

    with connection(settings.db_path) as conn:
        init_schema(conn)
        reporter = _ProgressReporter(conn, run_id, started_at)

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

        reporter.phase("checking_upstream")
        excluded = load_excluded_ats(_EXCLUDED_ATS_PATH)
        manifest = fetch_manifest(settings.manifest_url)
        candidate_slices = relevant_slices(manifest, excluded, conn)
        slices = changed_slices(conn, candidate_slices)

        if not slices:
            logger.info("no changed slices — nothing to do")
            reporter.finish()
            return PollResult(
                run_id=run_id,
                slices_processed=0,
                fetched_count=0,
                filtered_count=0,
                scored_count=0,
                failed_count=0,
                excluded_count=0,
                location_excluded_count=0,
                delisted_count=0,
                stale_by_live_data_count=0,
                csv_path=None,
            )

        seen_global, seen_req = load_seen_keys(conn)
        reporter.phase("queued", slice_total=len(slices))

        slices_processed = 0
        fetched_total = filtered_total = scored_total = failed_total = excluded_total = 0
        location_excluded_total = 0
        delisted_total = 0
        stale_by_live_data_total = 0
        all_records: list[JobRecord] = []
        csv_path: str | None = None

        # Module 29. The queue is re-derived from the database before every
        # slice rather than fixed when the run starts, and `changed_slices`
        # reads `slice_state` — so a source that finished drops out by itself,
        # one that gave way is still there (it never wrote its slice_state),
        # and one the operator asked to re-run appears mid-run. That last case
        # is the whole point: "run ashby instead" has to work even when this
        # run's original queue never contained ashby.
        failed_this_run: set[str] = set()
        preempted_names: list[str] = []
        held_names: set[str] = set()
        yields_left = max_yields_for(len(slices))

        try:
            while True:
                # A failed slice also leaves no slice_state, so without this it
                # would come back round forever.
                outstanding = [
                    item
                    for item in changed_slices(conn, candidate_slices)
                    if item.ats_type not in failed_this_run
                ]
                if not outstanding:
                    break

                runnable, held = order_slices(outstanding, read_control(conn))
                held_names.update(item.ats_type for item in held)
                if not runnable:
                    logger.info(
                        "every remaining source is held (%s) — nothing more to do",
                        ", ".join(sorted(held_names)),
                    )
                    break

                slice_info = runnable[0]
                should_yield = (
                    _make_yield_check(
                        conn, slice_info.ats_type, candidate_slices, failed_this_run
                    )
                    if yields_left > 0
                    else _never_yield
                )
                # Completions, not attempts, so a source that gave way and came
                # back doesn't make the page read "9 of 8". The total is
                # recomputed too, since the queue can grow mid-run.
                reporter.progress.slice_total = slices_processed + len(outstanding)
                reporter.slice_started(slices_processed + 1, slice_info)
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
                        reporter,
                        should_yield,
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
                    failed_this_run.add(slice_info.ats_type)
                    continue

                # Whatever it managed before giving way is already committed
                # and counts. What it does NOT do is count as processed —
                # its slice_state was never written, so it goes back in the
                # queue and will be picked up again once the source that
                # overtook it is done.
                fetched_total += stats.fetched
                filtered_total += stats.filtered
                scored_total += stats.scored
                failed_total += stats.failed
                excluded_total += stats.excluded
                location_excluded_total += stats.location_excluded
                delisted_total += stats.delisted
                stale_by_live_data_total += stats.stale_by_live_data
                all_records.extend(stats.records)

                if stats.preempted:
                    # No bookkeeping needed to requeue it: it never wrote a
                    # slice_state row, so the next pass through
                    # changed_slices finds it outstanding again.
                    yields_left -= 1
                    preempted_names.append(slice_info.ats_type)
                    logger.info(
                        "%s went back in the queue (%s); %d yield(s) left this run",
                        slice_info.ats_type, stats.yielded_to, yields_left,
                    )
                    continue

                slices_processed += 1

            if all_records:
                reporter.phase("exporting")
                run_date = _local_date(settings.timezone)
                csv_path = str(export_csv(all_records, settings.output_dir, run_date))
        finally:
            # In a `finally` on purpose. This used to sit after the loop, so a
            # run that stopped early — a budget halt, an unhandled error —
            # recorded nothing at all, and run_log is the ONLY durable record
            # of fetched/filtered counts (those rows are never persisted to
            # `jobs`; see the run_log comment in db.py). The real database
            # had 0 rows here while 3,700 jobs sat scored, which made the
            # dashboard's funnel read 0/0/0/0 forever.
            #
            # Guarded on slices_processed so the every-30-minutes no-op poll
            # doesn't bury the real runs under a drift of all-zero rows.
            # A SIGKILL still loses this — nothing can run then — which is
            # what the progress heartbeat is for.
            if slices_processed:
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
            reporter.finish()

        logger.info(
            "run_poll done: run_id=%s slices=%d fetched=%d filtered=%d scored=%d "
            "failed=%d excluded=%d location_excluded=%d delisted=%d stale_by_live_data=%d "
            "preempted=%s held=%s",
            run_id,
            slices_processed,
            fetched_total,
            filtered_total,
            scored_total,
            failed_total,
            excluded_total,
            location_excluded_total,
            delisted_total,
            stale_by_live_data_total,
            ",".join(preempted_names) or "-",
            ",".join(sorted(held_names)) or "-",
        )

        return PollResult(
            run_id=run_id,
            slices_processed=slices_processed,
            fetched_count=fetched_total,
            filtered_count=filtered_total,
            scored_count=scored_total,
            failed_count=failed_total,
            excluded_count=excluded_total,
            location_excluded_count=location_excluded_total,
            delisted_count=delisted_total,
            stale_by_live_data_count=stale_by_live_data_total,
            preempted=preempted_names,
            held=sorted(held_names),
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


def run_digest(settings: Settings, *, force: bool = False) -> bool:
    """Build and send today's digest. At most one per local day.

    **The once-a-day guard lives here, not in the caller.** It used to sit in
    the daemon, which meant anything else that called run_digest — cron, a
    second daemon, a test, a person at a shell — could send another. On
    2026-08-20 the test suite spawned this script seven times in three
    minutes and every one of them sent a real email, because the only thing
    stopping it was a check in a caller that wasn't involved.

    A guarantee enforced by every caller is not a guarantee. This one is
    enforced by the sender, so there is no path around it. `force=True` is
    the deliberate override, and it is never set by the daemon."""
    with connection(settings.db_path) as conn:
        init_schema(conn)

        if not force:
            since_str, until_str = local_day_bounds_utc(settings.timezone)
            if digest_sent_today(
                conn, datetime.fromisoformat(since_str), datetime.fromisoformat(until_str)
            ):
                logger.info(
                    "a digest already went out today (%s) — not sending another",
                    settings.timezone,
                )
                return True

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
        text_body = render_digest_text(sections, today)

        # A job can appear in both "strong" and "eligibility uncertain", so
        # email_log.job_count is a distinct count of jobs shown, not a sum
        # across sections. (The location section is disjoint from the rest now
        # that it is drawn from held-back rows, which are never scored.)
        shown = (
            sections.strong
            + sections.location_uncertain
            + sections.eligibility_uncertain
        )
        job_count = len({job.global_id for job in shown})
        subject = (
            f"Coldstart Digest — {today.isoformat()} — {len(sections.strong)} strong match(es)"
        )

        return send_digest(settings, html_body, subject, job_count, conn, text_body=text_body)


def run_liveness_sweep(settings: Settings) -> LivenessSweepResult:
    """Re-check already-SCORED, unactioned postings against their source ATS.

    The pre-LLM check in _process_slice only catches a posting that was
    already dead in the snapshot at scoring time. It cannot catch the case
    that actually triggered this module: a posting that was live when scored
    and died before anyone looked at it — hours or days later, since a
    'strong match' can sit unactioned until the next digest or a dashboard
    visit. This is the other half of the fix, run independently on its own
    schedule (daemon.py) rather than piggybacked on run_poll, since it hits
    third-party ATS endpoints directly and should not scale with poll
    frequency."""
    with connection(settings.db_path) as conn:
        init_schema(conn)

        candidates = scored_jobs_for_liveness_check(conn, CHECKED_ATS_TYPES)
        checked = 0
        delisted = 0

        for candidate in candidates:
            check = check_still_live(candidate.ats_type, candidate.apply_url, candidate.company)
            checked += 1
            if check.flag is LivenessFlag.DEAD:
                mark_delisted(conn, candidate.global_id, check.reason, datetime.now(UTC))
                delisted += 1

        logger.info(
            "liveness sweep done: checked=%d delisted=%d (of %d candidates)",
            checked,
            delisted,
            len(candidates),
        )
        return LivenessSweepResult(checked_count=checked, delisted_count=delisted)
