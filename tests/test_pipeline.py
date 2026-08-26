from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import httpx
import pandas as pd
import pytest
from conftest import FakeProvider
from freezegun import freeze_time

import coldstart.pipeline as pipeline
from coldstart import ats_control
from coldstart.db import (
    clear_slice_state,
    connection,
    get_slice_state,
    init_schema,
    set_job_state,
    set_slice_state,
    upsert_job,
)
from coldstart.fetcher import DownloadError
from coldstart.models import (
    EligibilityFlag,
    JobRecord,
    JobStatus,
    LocationFlag,
    RawJob,
    ResumeId,
    ScoreBand,
    SliceState,
)
from coldstart.pipeline import run_digest, run_liveness_sweep, run_poll
from coldstart.resume_ingest import NormalizedResume
from coldstart.settings import Settings

_SLOT_FILES = {
    ResumeId.A: "resume_a_swe.json",
    ResumeId.B: "resume_b_ai.json",
    ResumeId.C: "resume_c_fde_swe.json",
    ResumeId.D: "resume_d_fde_ai.json",
}


def _write_resume_slots(resumes_dir: Path) -> None:
    manifest = {}
    for slot, filename in _SLOT_FILES.items():
        record = NormalizedResume(
            resume_id=slot,
            source_filename=f"{filename}.pdf",
            source_sha256="0" * 64,
            extracted_at=datetime(2026, 1, 1, tzinfo=UTC),
            is_fde=slot in (ResumeId.C, ResumeId.D),
            is_ai=slot in (ResumeId.B, ResumeId.D),
            description=f"Slot {slot.value}",
            full_text=(
                f"Slot {slot.value}\nfake@example.com\n"
                f"WORK EXPERIENCE\nAcme Corp\n"
                f"• Built things with Python, AWS, distributed systems.\n"
                f"SKILLS\nPython, AWS\n"
            ),
        )
        (resumes_dir / filename).write_text(record.model_dump_json())
        manifest[slot.value] = {"file": filename, "description": record.description}
    (resumes_dir / "manifest.json").write_text(json.dumps(manifest))


@pytest.fixture
def make_settings(tmp_path):
    resumes_dir = tmp_path / "resumes"
    resumes_dir.mkdir()
    _write_resume_slots(resumes_dir)

    def _make(**overrides) -> Settings:
        kwargs = dict(
            experience_years=3.5,
            smtp_user="user@example.com",
            smtp_app_password="app-pw",
            digest_recipient="user@example.com",
            daily_token_spend_ceiling_usd=100.0,
            data_dir=tmp_path / "data",
            log_dir=tmp_path / "logs",
            output_dir=tmp_path / "output",
            resume_manifest=resumes_dir / "manifest.json",
            db_path=tmp_path / "coldstart.sqlite3",
        )
        kwargs.update(overrides)
        return Settings(**kwargs)

    return _make


@pytest.fixture
def settings(make_settings) -> Settings:
    return make_settings()


def _recent_iso(days_ago: int = 2) -> str:
    return (datetime.now(UTC) - timedelta(days=days_ago)).replace(tzinfo=None).isoformat()


def _sample_rows() -> list[dict]:
    # Relative, not a literal: the freshness filter measures age against
    # today, so a hardcoded date would silently start failing the suite
    # once it aged past MAX_POSTING_AGE_DAYS.
    common = dict(posted_at=_recent_iso(), raw=None)
    return [
        dict(
            ats_id="1",
            url="https://x/1",
            requisition_id="req-1",
            company="Acme",
            title="Software Engineer II",
            location="Remote — US",
            country_iso="US",
            is_remote=True,
            apply_url="https://x/1/apply",
            ats_type="greenhouse",
            description="Build backend services in Python.",
            **common,
        ),
        dict(
            ats_id="2",
            url="https://x/2",
            requisition_id="req-2",
            company="Acme",
            title="Sales Manager",
            location="Remote — US",
            country_iso="US",
            is_remote=True,
            apply_url="https://x/2/apply",
            ats_type="greenhouse",
            description="Manage a sales team.",
            **common,
        ),
        dict(
            ats_id="3",
            url="https://x/3",
            requisition_id="req-3",
            company="Beta",
            title="AI Engineer",
            location="Berlin, Germany",
            country_iso="DE",
            is_remote=False,
            apply_url="https://x/3/apply",
            ats_type="greenhouse",
            description="Build ML pipelines.",
            **common,
        ),
        dict(
            ats_id="4",
            url="https://x/4",
            requisition_id="req-4",
            company="Gamma",
            title="Software Engineer",
            location="New York, NY",
            country_iso="US",
            is_remote=False,
            apply_url="https://x/4/apply",
            ats_type="greenhouse",
            description="Requires active security clearance.",
            **common,
        ),
        dict(
            ats_id="5",
            url="https://x/5",
            requisition_id="req-5",
            company="Delta",
            title="AI Engineer",
            location="San Francisco, CA",
            country_iso="US",
            is_remote=False,
            apply_url="https://x/5/apply",
            ats_type="greenhouse",
            description="Build agentic pipelines using LLMs.",
            **common,
        ),
        # Location resolves to neither US nor foreign. Under default-deny this
        # is held back rather than scored, and must not reach the provider.
        dict(
            ats_id="6",
            url="https://x/6",
            requisition_id="req-6",
            company="Epsilon",
            title="Software Engineer",
            location="Remote",
            country_iso="",
            is_remote=None,
            apply_url="https://x/6/apply",
            ats_type="greenhouse",
            description="Build backend services in Python.",
            **common,
        ),
    ]


def _write_parquet(path: Path, rows: list[dict]) -> None:
    df = pd.DataFrame(rows)
    df["posted_at"] = pd.to_datetime(df["posted_at"])
    df.to_parquet(path)


def _manifest(ats_types: list[str], sha: str = "a" * 64) -> dict:
    return {
        "by_ats": {
            ats: {
                "parquet": f"https://example.com/{ats}.parquet",
                "parquet_sha256": sha,
                "parquet_size_bytes": 100,
                "rows": 5,
            }
            for ats in ats_types
        }
    }


def _score_json(score: int = 85, band: str = "strong", reasoning: str = "Good fit.") -> str:
    return json.dumps(
        {
            "eligible": True,
            "disqualification_reason": None,
            "score": score,
            "score_band": band,
            "tech_stack_match": score,
            "seniority_fit": score,
            "experience_fit": score,
            "role_type_fit": score,
            "matched_skills": ["Python"],
            "missing_skills": ["Go"],
            "reasoning": reasoning,
        }
    )


# --- run_poll: end-to-end --------------------------------------------------------


def test_end_to_end_run_scores_and_persists_expected_jobs(tmp_path, settings, monkeypatch):
    parquet_path = tmp_path / "greenhouse.parquet"
    _write_parquet(parquet_path, _sample_rows())

    provider = FakeProvider(
        [_score_json(score=85, band="strong"), _score_json(score=65, band="consider")]
    )
    monkeypatch.setattr(pipeline, "build_active_provider", lambda s: provider)
    monkeypatch.setattr(pipeline, "fetch_manifest", lambda url: _manifest(["greenhouse"]))
    monkeypatch.setattr(pipeline, "download_slice", lambda slice_info, data_dir, conn: parquet_path)

    result = run_poll(settings)

    assert result.fetched_count == 6
    assert result.filtered_count == 2  # survives title+location+eligibility: jobs 1 and 5
    assert result.scored_count == 2
    assert result.failed_count == 0
    assert result.excluded_count == 1  # job 4 (security clearance)
    assert result.location_excluded_count == 1  # job 6 (bare "Remote")
    assert result.slices_processed == 1
    assert result.csv_path is not None
    assert Path(result.csv_path).exists()

    with connection(settings.db_path) as conn:
        rows = conn.execute(
            "SELECT global_id, status, score, location_reason FROM jobs"
        ).fetchall()
        state = get_slice_state(conn, "greenhouse")

    by_id = {r["global_id"]: r for r in rows}
    assert by_id["greenhouse:1"]["status"] == "scored"
    assert by_id["greenhouse:1"]["score"] == 85
    assert by_id["greenhouse:4"]["status"] == "excluded"
    assert by_id["greenhouse:5"]["status"] == "scored"
    assert "greenhouse:2" not in by_id  # title-rejected, never persisted
    assert "greenhouse:3" not in by_id  # location-rejected, never persisted

    # Held back, not dropped: persisted with the rule that stopped it, no score,
    # and — the whole point of the filter — no provider call spent on it.
    assert by_id["greenhouse:6"]["status"] == "excluded_location"
    assert by_id["greenhouse:6"]["score"] is None
    assert by_id["greenhouse:6"]["location_reason"] == "bare_remote"
    assert provider.calls == 2

    assert state is not None
    assert state.last_sha256 == "a" * 64


def test_job_with_nan_optional_fields_is_cleaned_and_scored(tmp_path, settings, monkeypatch):
    # A string-typed column round-trips a None as a real float NaN once the
    # column also holds a real value elsewhere (verified directly) — the same
    # real-data gotcha noted elsewhere in this project (pandas NaN vs None).
    # A single-row all-null column instead round-trips as plain None, so two
    # rows (one with a value, one without) are needed to force that shape.
    has_value = dict(_sample_rows()[0])
    nan_valued = dict(_sample_rows()[4])
    nan_valued["requisition_id"] = None
    _write_parquet(tmp_path / "greenhouse.parquet", [has_value, nan_valued])
    parquet_path = tmp_path / "greenhouse.parquet"

    provider = FakeProvider([_score_json(score=85), _score_json(score=70)])
    monkeypatch.setattr(pipeline, "build_active_provider", lambda s: provider)
    monkeypatch.setattr(pipeline, "fetch_manifest", lambda url: _manifest(["greenhouse"]))
    monkeypatch.setattr(pipeline, "download_slice", lambda slice_info, data_dir, conn: parquet_path)

    result = run_poll(settings)

    assert result.scored_count == 2
    with connection(settings.db_path) as conn:
        row = conn.execute(
            "SELECT requisition_id, status FROM jobs WHERE global_id = 'greenhouse:5'"
        ).fetchone()
    assert row["requisition_id"] is None
    assert row["status"] == "scored"


@pytest.mark.parametrize("requisition_id,expected", [(5284, "5284"), (10492887.0, "10492887")])
def test_job_with_numeric_requisition_id_is_coerced_to_string(
    tmp_path, settings, monkeypatch, requisition_id, expected
):
    # Real-data finding: several live ATS feeds (amazon, cornerstone,
    # dayforce, paylocity, ...) store requisition_id as a bare int/float for
    # their entire slice, not a string — this used to crash the whole slice
    # (RawJob's pydantic `str` field rejects a raw int/float outright).
    row = dict(_sample_rows()[0])
    row["requisition_id"] = requisition_id
    _write_parquet(tmp_path / "greenhouse.parquet", [row])
    parquet_path = tmp_path / "greenhouse.parquet"

    provider = FakeProvider([_score_json(score=85)])
    monkeypatch.setattr(pipeline, "build_active_provider", lambda s: provider)
    monkeypatch.setattr(pipeline, "fetch_manifest", lambda url: _manifest(["greenhouse"]))
    monkeypatch.setattr(pipeline, "download_slice", lambda slice_info, data_dir, conn: parquet_path)

    result = run_poll(settings)

    assert result.scored_count == 1
    assert result.slices_processed == 1
    with connection(settings.db_path) as conn:
        row = conn.execute("SELECT requisition_id, status FROM jobs").fetchone()
    assert row["requisition_id"] == expected
    assert row["status"] == "scored"


def test_job_that_exhausts_all_providers_is_marked_failed(tmp_path, make_settings, monkeypatch):
    from coldstart.scoring.base import RateLimitError

    parquet_path = tmp_path / "greenhouse.parquet"
    _write_parquet(parquet_path, [_sample_rows()[0]])

    provider = FakeProvider([RateLimitError("simulated rate limit")])
    monkeypatch.setattr(pipeline, "build_active_provider", lambda s: provider)
    monkeypatch.setattr(pipeline, "fetch_manifest", lambda url: _manifest(["greenhouse"]))
    monkeypatch.setattr(pipeline, "download_slice", lambda slice_info, data_dir, conn: parquet_path)

    settings = make_settings(max_retries_per_provider=1)
    result = run_poll(settings)

    assert result.scored_count == 0
    assert result.failed_count == 1
    with connection(settings.db_path) as conn:
        row = conn.execute("SELECT status, score FROM jobs").fetchone()
    assert row["status"] == "failed"
    assert row["score"] is None


def test_every_persisted_job_has_terminal_status(tmp_path, settings, monkeypatch):
    parquet_path = tmp_path / "greenhouse.parquet"
    _write_parquet(parquet_path, _sample_rows())

    provider = FakeProvider([_score_json(score=85), _score_json(score=65)])
    monkeypatch.setattr(pipeline, "build_active_provider", lambda s: provider)
    monkeypatch.setattr(pipeline, "fetch_manifest", lambda url: _manifest(["greenhouse"]))
    monkeypatch.setattr(pipeline, "download_slice", lambda slice_info, data_dir, conn: parquet_path)

    run_poll(settings)

    with connection(settings.db_path) as conn:
        rows = conn.execute("SELECT status FROM jobs").fetchall()

    assert rows
    assert all(
        row["status"] in {"scored", "excluded", "excluded_location", "failed"} for row in rows
    )


# --- run_poll: change detection and failure isolation -----------------------------


def test_no_changed_slices_early_return_zero_llm_calls(settings, monkeypatch):
    with connection(settings.db_path) as conn:
        init_schema(conn)
        set_slice_state(
            conn,
            SliceState(
                ats_type="greenhouse",
                last_sha256="a" * 64,
                last_processed_at=datetime.now(UTC),
                row_count=5,
            ),
        )

    provider = FakeProvider([])

    def _boom(*args, **kwargs):
        raise AssertionError("download_slice must not be called when nothing changed")

    monkeypatch.setattr(pipeline, "build_active_provider", lambda s: provider)
    monkeypatch.setattr(pipeline, "fetch_manifest", lambda url: _manifest(["greenhouse"]))
    monkeypatch.setattr(pipeline, "download_slice", _boom)

    result = run_poll(settings)

    assert result.slices_processed == 0
    assert result.scored_count == 0
    assert result.csv_path is None
    assert provider.calls == 0


def test_one_slice_download_failure_others_still_process(tmp_path, settings, monkeypatch):
    parquet_path = tmp_path / "lever.parquet"
    _write_parquet(parquet_path, _sample_rows())

    provider = FakeProvider([_score_json(score=85), _score_json(score=65)])
    monkeypatch.setattr(pipeline, "build_active_provider", lambda s: provider)
    monkeypatch.setattr(
        pipeline, "fetch_manifest", lambda url: _manifest(["greenhouse", "lever"])
    )

    def _download(slice_info, data_dir, conn):
        if slice_info.ats_type == "greenhouse":
            raise DownloadError("simulated failure")
        return parquet_path

    monkeypatch.setattr(pipeline, "download_slice", _download)

    result = run_poll(settings)

    assert result.slices_processed == 1
    assert result.scored_count == 2

    with connection(settings.db_path) as conn:
        greenhouse_state = get_slice_state(conn, "greenhouse")
        lever_state = get_slice_state(conn, "lever")
        error_rows = conn.execute("SELECT stage, job_ref FROM errors").fetchall()

    assert greenhouse_state is None
    assert lever_state is not None
    assert any(r["stage"] == "poll" and r["job_ref"] == "greenhouse" for r in error_rows)


def test_mid_slice_crash_leaves_slice_state_unset_then_reprocesses(tmp_path, settings, monkeypatch):
    parquet_path = tmp_path / "greenhouse.parquet"
    rows = [_sample_rows()[0], _sample_rows()[4]]  # 2 scoreable jobs: greenhouse:1, greenhouse:5
    _write_parquet(parquet_path, rows)

    provider = FakeProvider([_score_json(score=85), _score_json(score=70)])
    monkeypatch.setattr(pipeline, "build_active_provider", lambda s: provider)
    monkeypatch.setattr(pipeline, "fetch_manifest", lambda url: _manifest(["greenhouse"]))
    monkeypatch.setattr(pipeline, "download_slice", lambda slice_info, data_dir, conn: parquet_path)

    call_count = {"n": 0}
    real_score_job = pipeline.score_job

    def _flaky_score_job(*args, **kwargs):
        call_count["n"] += 1
        if call_count["n"] == 2:
            raise RuntimeError("simulated mid-slice crash")
        return real_score_job(*args, **kwargs)

    monkeypatch.setattr(pipeline, "score_job", _flaky_score_job)

    run_poll(settings)  # job 1 persists; job 5's score_job call raises mid-slice

    with connection(settings.db_path) as conn:
        state_after_crash = get_slice_state(conn, "greenhouse")
        ids_after_crash = {r["global_id"] for r in conn.execute("SELECT global_id FROM jobs")}

    assert state_after_crash is None
    assert ids_after_crash == {"greenhouse:1"}

    result2 = run_poll(settings)  # re-processes: job 1 deduped, job 5 now scores successfully

    with connection(settings.db_path) as conn:
        state_after_retry = get_slice_state(conn, "greenhouse")
        rows_after_retry = conn.execute("SELECT global_id, status FROM jobs").fetchall()

    assert state_after_retry is not None
    assert state_after_retry.last_sha256 == "a" * 64
    assert {r["global_id"] for r in rows_after_retry} == {"greenhouse:1", "greenhouse:5"}
    assert all(r["status"] == "scored" for r in rows_after_retry)
    assert result2.scored_count == 1  # only the previously-crashed job counted this run
    assert call_count["n"] == 3


def test_second_run_with_republished_slice_scores_zero_new_jobs(tmp_path, settings, monkeypatch):
    parquet_path = tmp_path / "greenhouse.parquet"
    rows = [_sample_rows()[0], _sample_rows()[4]]
    _write_parquet(parquet_path, rows)

    provider = FakeProvider([_score_json(score=85), _score_json(score=70)])
    monkeypatch.setattr(pipeline, "build_active_provider", lambda s: provider)
    monkeypatch.setattr(pipeline, "download_slice", lambda slice_info, data_dir, conn: parquet_path)

    sha_holder = {"sha": "a" * 64}
    monkeypatch.setattr(
        pipeline, "fetch_manifest", lambda url: _manifest(["greenhouse"], sha=sha_holder["sha"])
    )

    result1 = run_poll(settings)
    assert result1.scored_count == 2

    # Simulate the upstream file being republished (new hash, same rows) — this
    # is the meaningful idempotency path: dedup, not the "nothing changed"
    # early-return, is what must prevent re-scoring.
    sha_holder["sha"] = "b" * 64
    result2 = run_poll(settings)

    assert result2.slices_processed == 1
    assert result2.filtered_count == 2
    assert result2.scored_count == 0
    assert result2.excluded_count == 0

    with connection(settings.db_path) as conn:
        ids = {r["global_id"] for r in conn.execute("SELECT global_id FROM jobs")}
    assert ids == {"greenhouse:1", "greenhouse:5"}


def test_budget_exceeded_halts_whole_run_without_marking_slice_state(
    tmp_path, make_settings, monkeypatch
):
    parquet_path = tmp_path / "greenhouse.parquet"
    rows = [_sample_rows()[0], _sample_rows()[4]]
    _write_parquet(parquet_path, rows)

    settings = make_settings(daily_token_spend_ceiling_usd=0.0)

    provider = FakeProvider([_score_json(score=85)])
    monkeypatch.setattr(pipeline, "build_active_provider", lambda s: provider)
    monkeypatch.setattr(pipeline, "fetch_manifest", lambda url: _manifest(["greenhouse"]))
    monkeypatch.setattr(pipeline, "download_slice", lambda slice_info, data_dir, conn: parquet_path)

    from coldstart.budget import BudgetExceeded

    with pytest.raises(BudgetExceeded):
        run_poll(settings)

    with connection(settings.db_path) as conn:
        state = get_slice_state(conn, "greenhouse")
        job_rows = conn.execute("SELECT global_id FROM jobs").fetchall()

    assert state is None
    assert job_rows == []
    assert provider.calls == 0  # check_budget raises before any LLM call


# --- run_digest --------------------------------------------------------------------


def test_run_digest_after_poll_sends_with_correct_footer_stats(
    tmp_path, settings, monkeypatch, mocker
):
    parquet_path = tmp_path / "greenhouse.parquet"
    _write_parquet(parquet_path, _sample_rows())

    provider = FakeProvider(
        [_score_json(score=85, band="strong"), _score_json(score=65, band="consider")]
    )
    monkeypatch.setattr(pipeline, "build_active_provider", lambda s: provider)
    monkeypatch.setattr(pipeline, "fetch_manifest", lambda url: _manifest(["greenhouse"]))
    monkeypatch.setattr(pipeline, "download_slice", lambda slice_info, data_dir, conn: parquet_path)

    poll_result = run_poll(settings)
    assert poll_result.scored_count == 2

    mock_smtp_cls = mocker.patch("coldstart.digest.smtplib.SMTP")
    mock_smtp = mock_smtp_cls.return_value.__enter__.return_value

    sent = run_digest(settings)

    assert sent is True
    mock_smtp.send_message.assert_called_once()
    message = mock_smtp.send_message.call_args[0][0]
    html_part = next(p for p in message.walk() if p.get_content_type() == "text/html")
    html_content = html_part.get_content()

    assert "Acme" in html_content  # strong section
    assert "Delta" in html_content  # consider section
    assert "Fetched: 6" in html_content
    assert "Filtered: 2" in html_content
    assert "Scored: 2" in html_content
    assert "fake" in html_content  # provider name, from spend_log

    with connection(settings.db_path) as conn:
        email_row = conn.execute("SELECT status, job_count FROM email_log").fetchone()
    assert email_row["status"] == "sent"
    # 2 scored + the 1 job held back by the location filter, which the digest
    # now surfaces in its own section.
    assert email_row["job_count"] == 3


def test_blocked_company_is_never_scored_persisted_or_sent_to_an_llm(
    settings, tmp_path, monkeypatch
):
    """The hard guarantee for the block list (filters/company.py).

    excluded_ats.json stops these employers' own feeds from being downloaded,
    but not the same employer posting through someone else's platform —
    observed live as `amazon.jobs.personio.com` on the personio slice, and as
    `uberfreight`/`googlefiber` on greenhouse, 8 of whose postings survive the
    title filter. This asserts the second gate holds end to end. Uses the
    `lever` ats_type rather than personio (personio is now excluded from
    scope) purely as a stand-in in-scope source; the block-list logic under
    test doesn't care which ATS the row came from."""
    rows = [
        dict(
            ats_id="blocked-1",
            url="https://x/b1",
            requisition_id="req-b1",
            company="amazon.jobs.personio.com",
            title="Software Engineer",
            location="Remote — US",
            country_iso="US",
            is_remote=True,
            apply_url="https://x/b1/apply",
            ats_type="lever",
            description="Build backend services in Python.",
            posted_at=_recent_iso(),
            raw=None,
        ),
        dict(
            ats_id="blocked-2",
            url="https://x/b2",
            requisition_id="req-b2",
            company="uberfreight",
            title="Senior Software Engineer",
            location="Chicago, IL",
            country_iso="US",
            is_remote=False,
            apply_url="https://x/b2/apply",
            ats_type="lever",
            description="Logistics platform work.",
            posted_at=_recent_iso(),
            raw=None,
        ),
        # A look-alike that must survive: a real roofing company, 58 real
        # postings in the live data.
        dict(
            ats_id="kept-1",
            url="https://x/k1",
            requisition_id="req-k1",
            company="apple-roofing",
            # Specific enough to route by keyword — a bare "Software Engineer"
            # escalates to an LLM routing call (Module 11) and would muddy the
            # call count this test is asserting on.
            title="Senior Software Engineer",
            location="Austin, TX",
            country_iso="US",
            is_remote=False,
            apply_url="https://x/k1/apply",
            ats_type="lever",
            description="Build internal tooling.",
            posted_at=_recent_iso(),
            raw=None,
        ),
    ]
    _write_parquet(tmp_path / "lever.parquet", rows)

    provider = FakeProvider([_score_json(80, "strong", "Good fit.")])
    monkeypatch.setattr(pipeline, "build_active_provider", lambda s: provider)
    monkeypatch.setattr(pipeline, "fetch_manifest", lambda url: _manifest(["lever"], "sha-1"))
    monkeypatch.setattr(
        pipeline,
        "download_slice",
        lambda slice_info, data_dir, conn: tmp_path / "lever.parquet",
    )

    result = pipeline.run_poll(settings)

    # Exactly one LLM call: the roofing company. Neither blocked row cost a cent.
    assert provider.calls == 1
    assert result.scored_count == 1

    with connection(settings.db_path) as conn:
        companies = {row[0] for row in conn.execute("SELECT company FROM jobs")}
    assert companies == {"apple-roofing"}


# --- liveness check (Module 27) ---------------------------------------------

_DEAD_WORKDAY_URL = (
    "https://genpact.wd108.myworkdayjobs.com/External_Careers/job/"
    "3409-GLLC-1155-Perimeter-Center-West-Atlanta-GA/AI-Engineer-4A_JR10018694"
)


def _workday_row(**overrides) -> dict:
    row = dict(
        ats_id="dead-1",
        url=_DEAD_WORKDAY_URL,
        requisition_id="JR10018694",
        company="Genpact",
        title="AI Engineer 4A",
        location="Atlanta, GA",
        country_iso="US",
        is_remote=False,
        apply_url=_DEAD_WORKDAY_URL,
        ats_type="workday",
        description="Build agentic pipelines.",
        posted_at=_recent_iso(),
        raw=None,
    )
    row.update(overrides)
    return row


def test_a_dead_workday_posting_is_delisted_before_scoring(tmp_path, settings, monkeypatch):
    """The trigger case, as a test: a posting that survives every other filter
    but the source ATS confirms is gone must never reach the LLM, and must
    persist as DELISTED with the check's reason — not silently vanish and not
    silently score."""
    _write_parquet(tmp_path / "workday.parquet", [_workday_row()])

    provider = FakeProvider([])  # scoring this job would be the bug
    monkeypatch.setattr(pipeline, "build_active_provider", lambda s: provider)
    monkeypatch.setattr(pipeline, "fetch_manifest", lambda url: _manifest(["workday"]))
    monkeypatch.setattr(
        pipeline, "download_slice", lambda slice_info, data_dir, conn: tmp_path / "workday.parquet"
    )
    # Real response shape confirmed live, 2026-08-24, for this exact URL.
    monkeypatch.setattr(
        httpx, "get", lambda *a, **k: httpx.Response(403, json={"errorCode": "S22"})
    )

    result = run_poll(settings)

    assert result.filtered_count == 1  # survives title/location/eligibility/freshness
    assert result.scored_count == 0
    assert result.delisted_count == 1
    assert provider.calls == 0

    with connection(settings.db_path) as conn:
        row = conn.execute(
            "SELECT status, score, delist_reason FROM jobs WHERE global_id = 'workday:dead-1'"
        ).fetchone()
    assert row["status"] == "delisted"
    assert row["score"] is None
    assert row["delist_reason"] == "workday_cxs_403"


def test_a_live_but_stale_workday_posting_is_dropped_before_scoring(
    tmp_path, settings, monkeypatch
):
    """The follow-up idea: Workday's own CXS response already carries a live
    "Posted N Days Ago" string, more accurate than the snapshot's posted_at
    that filter_freshness ran against upstream of here. A posting that's
    genuinely still live but older than MAX_POSTING_AGE_DAYS per that live
    signal gets the same treatment as any other stale posting — dropped, not
    scored, not persisted — just from a better source."""
    assert settings.max_posting_age_days == 15
    _write_parquet(tmp_path / "workday.parquet", [_workday_row()])

    provider = FakeProvider([])  # scoring this job would be the bug
    monkeypatch.setattr(pipeline, "build_active_provider", lambda s: provider)
    monkeypatch.setattr(pipeline, "fetch_manifest", lambda url: _manifest(["workday"]))
    monkeypatch.setattr(
        pipeline, "download_slice", lambda slice_info, data_dir, conn: tmp_path / "workday.parquet"
    )
    monkeypatch.setattr(
        httpx,
        "get",
        lambda *a, **k: httpx.Response(
            200,
            json={
                "jobPostingInfo": {
                    "canApply": True,
                    "posted": True,
                    "postedOn": "Posted 24 Days Ago",
                }
            },
        ),
    )

    result = run_poll(settings)

    assert result.scored_count == 0
    assert result.delisted_count == 0
    assert result.stale_by_live_data_count == 1
    assert provider.calls == 0

    with connection(settings.db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM jobs WHERE global_id = 'workday:dead-1'"
        ).fetchone()
    assert row is None  # dropped, not persisted — same as filter_freshness


def test_a_live_workday_posting_within_the_freshness_window_is_scored(
    tmp_path, settings, monkeypatch
):
    _write_parquet(tmp_path / "workday.parquet", [_workday_row()])

    provider = FakeProvider([_score_json(90, "strong")])
    monkeypatch.setattr(pipeline, "build_active_provider", lambda s: provider)
    monkeypatch.setattr(pipeline, "fetch_manifest", lambda url: _manifest(["workday"]))
    monkeypatch.setattr(
        pipeline, "download_slice", lambda slice_info, data_dir, conn: tmp_path / "workday.parquet"
    )
    monkeypatch.setattr(
        httpx,
        "get",
        lambda *a, **k: httpx.Response(
            200,
            json={
                "jobPostingInfo": {
                    "canApply": True,
                    "posted": True,
                    "postedOn": "Posted 3 Days Ago",
                }
            },
        ),
    )

    result = run_poll(settings)

    assert result.scored_count == 1
    assert result.stale_by_live_data_count == 0
    assert provider.calls == 1


def test_liveness_check_disabled_scores_the_job_normally(tmp_path, make_settings, monkeypatch):
    """The settings escape hatch: a real HTTP call per checkable job against a
    third-party site should be possible to switch off in one place."""
    settings = make_settings(liveness_check_enabled=False)
    _write_parquet(tmp_path / "workday.parquet", [_workday_row()])

    provider = FakeProvider([_score_json(90, "strong")])
    monkeypatch.setattr(pipeline, "build_active_provider", lambda s: provider)
    monkeypatch.setattr(pipeline, "fetch_manifest", lambda url: _manifest(["workday"]))
    monkeypatch.setattr(
        pipeline, "download_slice", lambda slice_info, data_dir, conn: tmp_path / "workday.parquet"
    )
    monkeypatch.setattr(
        httpx, "get", lambda *a, **k: pytest.fail("liveness check must not run when disabled")
    )

    result = run_poll(settings)

    assert result.scored_count == 1
    assert result.delisted_count == 0
    assert provider.calls == 1


def test_run_liveness_sweep_delists_a_dead_scored_job_and_preserves_its_score(
    settings, monkeypatch
):
    """The other half of the fix: a job that was live when scored and died
    afterward — exactly what happened to the real Genpact posting that
    prompted this module."""
    with connection(settings.db_path) as conn:
        init_schema(conn)
        upsert_job(
            conn,
            JobRecord(
                global_id="workday:dead-1",
                requisition_id="JR10018694",
                company="Genpact",
                title="AI Engineer 4A",
                ats_type="workday",
                apply_url=_DEAD_WORKDAY_URL,
                status=JobStatus.SCORED,
                score=95,
                score_band=ScoreBand.STRONG,
                reasoning="Strong match.",
                location_flag=LocationFlag.ACCEPTED,
                eligibility_flag=EligibilityFlag.PASSED,
                first_seen_at=datetime(2026, 8, 24, tzinfo=UTC),
                scored_at=datetime(2026, 8, 24, tzinfo=UTC),
            ),
        )

    monkeypatch.setattr(
        httpx, "get", lambda *a, **k: httpx.Response(403, json={"errorCode": "S22"})
    )

    result = run_liveness_sweep(settings)

    assert result.checked_count == 1
    assert result.delisted_count == 1

    with connection(settings.db_path) as conn:
        row = conn.execute(
            "SELECT status, score, reasoning, delist_reason FROM jobs"
            " WHERE global_id = 'workday:dead-1'"
        ).fetchone()
    assert row["status"] == "delisted"
    assert row["score"] == 95  # preserved, not clobbered by the sweep
    assert row["reasoning"] == "Strong match."
    assert row["delist_reason"] == "workday_cxs_403"


def test_run_liveness_sweep_never_checks_a_job_the_operator_already_acted_on(
    settings, monkeypatch
):
    with connection(settings.db_path) as conn:
        init_schema(conn)
        upsert_job(
            conn,
            JobRecord(
                global_id="workday:applied-1",
                company="Genpact",
                title="AI Engineer",
                ats_type="workday",
                apply_url=_DEAD_WORKDAY_URL,
                status=JobStatus.SCORED,
                score=90,
                score_band=ScoreBand.STRONG,
                location_flag=LocationFlag.ACCEPTED,
                eligibility_flag=EligibilityFlag.PASSED,
                first_seen_at=datetime(2026, 8, 24, tzinfo=UTC),
                scored_at=datetime(2026, 8, 24, tzinfo=UTC),
            ),
        )
        set_job_state(conn, "workday:applied-1", "applied")

    monkeypatch.setattr(
        httpx, "get", lambda *a, **k: pytest.fail("must not check an already-actioned job")
    )

    result = run_liveness_sweep(settings)

    assert result.checked_count == 0
    assert result.delisted_count == 0


def test_stale_postings_never_reach_the_llm(settings, tmp_path, monkeypatch):
    """Freshness gate, end to end. Measured on real data, ~95% of everything
    that survives the title filter is older than 15 days, so this is the
    largest single lever on LLM spend."""
    common = dict(raw=None, country_iso="US", is_remote=True, ats_type="greenhouse")
    rows = [
        dict(
            ats_id="fresh",
            url="https://x/f",
            requisition_id="req-f",
            company="Acme",
            title="Senior Software Engineer",
            location="Remote — US",
            apply_url="https://x/f/apply",
            description="Build backend services in Python.",
            posted_at="2026-08-19T00:00:00",
            **common,
        ),
        dict(
            ats_id="stale",
            url="https://x/s",
            requisition_id="req-s",
            company="Acme",
            title="Senior Software Engineer",
            location="Remote — US",
            apply_url="https://x/s/apply",
            description="Build backend services in Python.",
            posted_at="2026-01-15T00:00:00",
            **common,
        ),
        dict(
            ats_id="undated",
            url="https://x/u",
            requisition_id="req-u",
            company="Acme",
            title="Senior Software Engineer",
            location="Remote — US",
            apply_url="https://x/u/apply",
            description="Build backend services in Python.",
            posted_at=None,
            **common,
        ),
    ]
    _write_parquet(tmp_path / "greenhouse.parquet", rows)

    provider = FakeProvider(
        [_score_json(80, "strong", "Good fit."), _score_json(75, "strong", "Fine.")]
    )
    monkeypatch.setattr(pipeline, "build_active_provider", lambda s: provider)
    monkeypatch.setattr(pipeline, "fetch_manifest", lambda url: _manifest(["greenhouse"], "sha-1"))
    monkeypatch.setattr(
        pipeline,
        "download_slice",
        lambda slice_info, data_dir, conn: tmp_path / "greenhouse.parquet",
    )

    with freeze_time("2026-08-20T12:00:00Z"):
        result = pipeline.run_poll(settings)

    # The fresh one and the undated one; never the stale one.
    assert provider.calls == 2
    assert result.scored_count == 2
    with connection(settings.db_path) as conn:
        ids = {row[0] for row in conn.execute("SELECT global_id FROM jobs")}
    assert ids == {"greenhouse:fresh", "greenhouse:undated"}


# --- digest coverage is continuous (Module 18 addendum) -----------------------------


def _seed_scored_job(settings, global_id, scored_at, score=85, company="Evening Corp"):
    from coldstart.models import (
        EligibilityFlag,
        JobRecord,
        JobStatus,
        LocationFlag,
        ScoreBand,
    )

    with connection(settings.db_path) as conn:
        init_schema(conn)
        conn.execute("DELETE FROM jobs WHERE global_id = ?", (global_id,))
        from coldstart.db import upsert_job

        upsert_job(
            conn,
            JobRecord(
                global_id=global_id,
                requisition_id=None,
                company=company,
                title="Senior Software Engineer",
                location="Austin, TX",
                apply_url="https://x/apply",
                ats_type="greenhouse",
                posted_at=scored_at,
                resume_used=ResumeId.A,
                score=score,
                score_band=ScoreBand.STRONG,
                eligible=True,
                matched_skills=["Python"],
                missing_skills=[],
                reasoning="Strong overlap.",
                status=JobStatus.SCORED,
                location_flag=LocationFlag.ACCEPTED,
                eligibility_flag=EligibilityFlag.PASSED,
                provider_used="fake",
                first_seen_at=scored_at,
                scored_at=scored_at,
            ),
        )


def _sent_html(mocker, settings, *, force=False):
    mock_smtp_cls = mocker.patch("coldstart.digest.smtplib.SMTP")
    mock_smtp = mock_smtp_cls.return_value.__enter__.return_value
    sent = run_digest(settings, force=force)
    if not mock_smtp.send_message.called:
        return sent, ""
    message = mock_smtp.send_message.call_args[0][0]
    part = next(p for p in message.walk() if p.get_content_type() == "text/html")
    return sent, part.get_content()


def test_a_job_found_during_the_day_still_reaches_the_next_digest(
    settings, mocker
):
    """The blind-spot regression.

    The window used to be local midnight, so an 08:00 digest reported only the
    overnight hours and everything found between 08:00 and midnight was never
    emailed at all. This job is scored at 19:50 local — right in that old
    gap — and must appear in the next morning's digest."""
    evening = datetime(2026, 8, 20, 2, 50, tzinfo=UTC)  # 19:50 the previous day, PT
    _seed_scored_job(settings, "greenhouse:evening", evening)

    # 08:00 PT the next morning, after local midnight has rolled over.
    with freeze_time("2026-08-20T15:00:00Z"):
        sent, html_content = _sent_html(mocker, settings)

    assert sent is True
    assert "Evening Corp" in html_content


def test_the_window_starts_where_the_previous_digest_ended(settings, mocker):
    """Two consecutive digests must not repeat a job, and must not skip one."""
    first_job = datetime(2026, 8, 20, 2, 0, tzinfo=UTC)
    _seed_scored_job(settings, "greenhouse:first", first_job, score=91, company="FirstCo")

    with freeze_time("2026-08-20T15:00:00Z"):
        _sent, first_html = _sent_html(mocker, settings)
    assert "FirstCo" in first_html

    # A second job lands after that digest went out.
    second_job = datetime(2026, 8, 20, 18, 0, tzinfo=UTC)
    _seed_scored_job(settings, "greenhouse:second", second_job, score=77, company="SecondCo")

    # force=True because the once-a-day guard would otherwise (correctly)
    # refuse this second send; the window is what's under test here.
    with freeze_time("2026-08-20T20:00:00Z"):
        _sent, second_html = _sent_html(mocker, settings, force=True)

    # The second digest covers only what arrived since the first one: no gap,
    # and no repeat of what was already reported.
    assert "SecondCo" in second_html
    assert "FirstCo" not in second_html


def test_a_failed_send_means_the_next_digest_covers_both_periods(settings, mocker):
    """A failed digest must not advance the window, or its jobs are lost to
    every future digest as well."""
    job = datetime(2026, 8, 20, 2, 0, tzinfo=UTC)
    _seed_scored_job(settings, "greenhouse:unreported", job, score=88, company="UnreportedCo")

    # First attempt fails at the SMTP layer.
    import smtplib

    failing = mocker.patch("coldstart.digest.smtplib.SMTP")
    failing.return_value.__enter__.side_effect = smtplib.SMTPException("smtp down")
    with freeze_time("2026-08-20T15:00:00Z"):
        assert run_digest(settings) is False

    # A later attempt must still include it.
    # No force needed: the failed attempt wrote status='failed', which must
    # not count as "already sent today".
    mocker.stopall()
    with freeze_time("2026-08-20T20:00:00Z"):
        sent, html_content = _sent_html(mocker, settings)
    assert sent is True
    assert "UnreportedCo" in html_content


def test_the_footer_states_the_period_covered(settings, mocker):
    _seed_scored_job(settings, "greenhouse:footer", datetime(2026, 8, 20, 2, 0, tzinfo=UTC))
    with freeze_time("2026-08-20T15:00:00Z"):
        _sent, html_content = _sent_html(mocker, settings)
    assert "Covering everything since" in html_content


# --- apply links -----------------------------------------------------------


def test_apply_url_falls_back_to_the_posting_url_when_absent(settings, tmp_path, monkeypatch):
    """Real-data bug: every one of amazon's 33,888 rows has apply_url as NaN,
    so a digest built from that slice showed an empty Apply column for every
    job — while `url` sat right there, populated, and was being discarded."""
    common = dict(raw=None, country_iso="US", is_remote=True, ats_type="greenhouse")
    rows = [
        dict(
            ats_id="noapply",
            url="https://account.amazon.jobs/jobs/10495450",
            requisition_id="req-1",
            company="Acme",
            title="Senior Software Engineer",
            location="Remote — US",
            apply_url=None,  # what amazon's whole slice looks like
            description="Build backend services in Python.",
            posted_at=_recent_iso(),
            **common,
        ),
        dict(
            ats_id="hasapply",
            url="https://jobs.ashbyhq.com/acme/456",
            requisition_id="req-2",
            company="Acme",
            title="Senior Backend Engineer",
            location="Remote — US",
            apply_url="https://jobs.ashbyhq.com/acme/456/application",
            description="Build backend services in Python.",
            posted_at=_recent_iso(),
            **common,
        ),
    ]
    _write_parquet(tmp_path / "greenhouse.parquet", rows)

    provider = FakeProvider([_score_json(85, "strong"), _score_json(80, "strong")])
    monkeypatch.setattr(pipeline, "build_active_provider", lambda s: provider)
    monkeypatch.setattr(pipeline, "fetch_manifest", lambda url: _manifest(["greenhouse"]))
    monkeypatch.setattr(
        pipeline,
        "download_slice",
        lambda slice_info, data_dir, conn: tmp_path / "greenhouse.parquet",
    )

    run_poll(settings)

    with connection(settings.db_path) as conn:
        links = dict(conn.execute("SELECT global_id, apply_url FROM jobs"))

    # Falls back to the posting URL rather than leaving the operator with nothing.
    assert links["greenhouse:noapply"] == "https://account.amazon.jobs/jobs/10495450"
    # A real apply link is always preferred over the posting page.
    assert links["greenhouse:hasapply"] == "https://jobs.ashbyhq.com/acme/456/application"


def test_a_second_digest_in_one_day_is_refused(settings, mocker):
    """Strictly one email per local day, enforced by the sender.

    The 2026-08-20 incident: the guard lived in the daemon, so seven other
    callers sent seven real emails in three minutes. A guarantee that every
    caller has to remember is not a guarantee."""
    _seed_scored_job(settings, "greenhouse:once", datetime(2026, 8, 20, 2, 0, tzinfo=UTC))

    with freeze_time("2026-08-20T15:00:00Z"):
        first_cls = mocker.patch("coldstart.digest.smtplib.SMTP")
        assert run_digest(settings) is True
        assert first_cls.return_value.__enter__.return_value.send_message.call_count == 1

    # Six more attempts, as the test suite did. None may reach SMTP.
    with freeze_time("2026-08-20T15:20:00Z"):
        again_cls = mocker.patch("coldstart.digest.smtplib.SMTP")
        for _ in range(6):
            assert run_digest(settings) is True     # reports success, sends nothing
        assert again_cls.return_value.__enter__.return_value.send_message.call_count == 0

    with connection(settings.db_path) as conn:
        assert conn.execute("SELECT COUNT(*) FROM email_log").fetchone()[0] == 1


def test_the_next_local_day_sends_again(settings, mocker):
    _seed_scored_job(settings, "greenhouse:day1", datetime(2026, 8, 20, 2, 0, tzinfo=UTC))
    with freeze_time("2026-08-20T15:00:00Z"):
        mocker.patch("coldstart.digest.smtplib.SMTP")
        assert run_digest(settings) is True

    _seed_scored_job(settings, "greenhouse:day2", datetime(2026, 8, 21, 2, 0, tzinfo=UTC))
    with freeze_time("2026-08-21T15:00:00Z"):
        cls = mocker.patch("coldstart.digest.smtplib.SMTP")
        assert run_digest(settings) is True
        assert cls.return_value.__enter__.return_value.send_message.call_count == 1


# --- run_log and the poll heartbeat (Module 28) --------------------------------------


def _run_log_rows(settings):
    with connection(settings.db_path) as conn:
        return conn.execute(
            "SELECT fetched_count, filtered_count, scored_count, failed_count FROM run_log"
        ).fetchall()


def test_a_run_that_dies_after_processing_slices_still_records_its_funnel_row(
    tmp_path, settings, monkeypatch
):
    """log_run sits in a `finally` now.

    It used to run only after the loop returned normally, so a run that
    stopped early recorded nothing — and run_log is the ONLY durable record
    of fetched/filtered counts, since those rows are deliberately never
    persisted to `jobs`. The real database had zero rows here next to 3,700
    scored jobs, which made the dashboard's funnel read 0/0/0/0 forever."""
    parquet_path = tmp_path / "greenhouse.parquet"
    _write_parquet(parquet_path, _sample_rows())

    provider = FakeProvider([_score_json(score=85), _score_json(score=65, band="consider")])
    monkeypatch.setattr(pipeline, "build_active_provider", lambda s: provider)
    monkeypatch.setattr(pipeline, "fetch_manifest", lambda url: _manifest(["greenhouse"]))
    monkeypatch.setattr(pipeline, "download_slice", lambda slice_info, data_dir, conn: parquet_path)

    def _explode(*args, **kwargs):
        raise OSError("no space left on device")

    monkeypatch.setattr(pipeline, "export_csv", _explode)

    with pytest.raises(OSError):
        run_poll(settings)

    rows = _run_log_rows(settings)
    assert len(rows) == 1
    assert (rows[0]["fetched_count"], rows[0]["scored_count"]) == (6, 2)


def test_a_no_op_poll_does_not_write_an_all_zero_run_log_row(tmp_path, settings, monkeypatch):
    """A poll runs every 30 minutes and usually finds nothing changed. If
    those wrote rows, the real runs would drown in a drift of zeroes."""
    monkeypatch.setattr(pipeline, "build_active_provider", lambda s: FakeProvider([]))
    monkeypatch.setattr(pipeline, "fetch_manifest", lambda url: _manifest(["greenhouse"]))

    with connection(settings.db_path) as conn:
        init_schema(conn)
        set_slice_state(
            conn,
            SliceState(
                ats_type="greenhouse",
                last_sha256="a" * 64,          # matches _manifest's default sha
                last_processed_at=datetime.now(UTC),
                row_count=5,
            ),
        )

    result = run_poll(settings)
    assert result.slices_processed == 0
    assert _run_log_rows(settings) == []


def test_run_poll_reports_its_progress_and_marks_it_finished(tmp_path, settings, monkeypatch):
    parquet_path = tmp_path / "greenhouse.parquet"
    _write_parquet(parquet_path, _sample_rows())

    provider = FakeProvider([_score_json(score=85), _score_json(score=65, band="consider")])
    monkeypatch.setattr(pipeline, "build_active_provider", lambda s: provider)
    monkeypatch.setattr(pipeline, "fetch_manifest", lambda url: _manifest(["greenhouse"]))
    monkeypatch.setattr(pipeline, "download_slice", lambda slice_info, data_dir, conn: parquet_path)

    run_poll(settings)

    from coldstart.progress import read_progress

    with connection(settings.db_path) as conn:
        progress = read_progress(conn)

    assert progress is not None
    assert progress.finished_at is not None
    assert progress.is_running is False        # finished, so never "running"
    assert progress.phase == "done"
    assert progress.slice_total == 1
    assert progress.scored == 2
    assert progress.excluded == 1              # the security-clearance row
    assert progress.location_excluded == 1     # the bare "Remote" row


def test_a_no_op_poll_still_closes_out_its_heartbeat(tmp_path, settings, monkeypatch):
    """Otherwise the previous run's heartbeat would sit there unfinished and
    a reader would keep calling a long-dead poll "interrupted"."""
    monkeypatch.setattr(pipeline, "build_active_provider", lambda s: FakeProvider([]))
    monkeypatch.setattr(pipeline, "fetch_manifest", lambda url: _manifest(["greenhouse"]))

    with connection(settings.db_path) as conn:
        init_schema(conn)
        set_slice_state(
            conn,
            SliceState(
                ats_type="greenhouse",
                last_sha256="a" * 64,
                last_processed_at=datetime.now(UTC),
                row_count=5,
            ),
        )

    run_poll(settings)

    from coldstart.progress import read_progress

    with connection(settings.db_path) as conn:
        progress = read_progress(conn)
    assert progress is not None and progress.finished_at is not None


# --- the operator's queue control (Module 29) -----------------------------------------


def _rows_for(ats_type: str) -> list[dict]:
    """The sample rows relabelled for another source.

    global_id is synthesised as `{ats_type}:{ats_id}` (fetcher.load_slice).
    requisition_id has to move too: dedupe's second key is
    (company, requisition_id, location), so leaving it alone would make every
    lever row a duplicate of its greenhouse twin and silently swallow the
    whole second slice."""
    return [
        {**row, "ats_type": ats_type, "requisition_id": f"{ats_type}-{row['requisition_id']}"}
        for row in _sample_rows()
    ]


def _two_slice_setup(tmp_path, monkeypatch, provider):
    greenhouse = tmp_path / "greenhouse.parquet"
    lever = tmp_path / "lever.parquet"
    _write_parquet(greenhouse, _rows_for("greenhouse"))
    _write_parquet(lever, _rows_for("lever"))

    monkeypatch.setattr(pipeline, "build_active_provider", lambda s: provider)
    monkeypatch.setattr(
        pipeline, "fetch_manifest", lambda url: _manifest(["greenhouse", "lever"])
    )
    monkeypatch.setattr(
        pipeline,
        "download_slice",
        lambda slice_info, data_dir, conn: (
            greenhouse if slice_info.ats_type == "greenhouse" else lever
        ),
    )


def test_a_held_source_is_never_processed(tmp_path, settings, monkeypatch):
    provider = FakeProvider([_score_json(score=85), _score_json(score=65)])
    _two_slice_setup(tmp_path, monkeypatch, provider)

    with connection(settings.db_path) as conn:
        init_schema(conn)
        ats_control.hold(conn, "greenhouse")

    result = run_poll(settings)

    assert result.slices_processed == 1
    assert result.held == ["greenhouse"]
    with connection(settings.db_path) as conn:
        # Untouched, so still outstanding — a hold is a pause, not a skip.
        assert get_slice_state(conn, "greenhouse") is None
        assert get_slice_state(conn, "lever") is not None


def test_priority_decides_which_source_runs_first(tmp_path, settings, monkeypatch):
    order: list[str] = []
    provider = FakeProvider([_score_json(score=85)] * 4)
    _two_slice_setup(tmp_path, monkeypatch, provider)

    real_download = pipeline.download_slice

    def _record(slice_info, data_dir, conn):
        order.append(slice_info.ats_type)
        return real_download(slice_info, data_dir, conn)

    monkeypatch.setattr(pipeline, "download_slice", _record)

    with connection(settings.db_path) as conn:
        init_schema(conn)
        ats_control.run_next(conn, "lever")

    run_poll(settings)
    assert order == ["lever", "greenhouse"]


def _reorder_after_first_score(settings, action, target: str):
    """Run `action` on the control row the first time a job is scored.

    This is the real timing: the operator clicks while a slice is already
    part-way through, which is precisely the case a queue re-read at slice
    boundaries alone would miss."""
    real_score_job = pipeline.score_job
    fired: list[bool] = []

    def _scoring(job, resume_text, provider, conn, settings_arg):
        if not fired:
            fired.append(True)
            action(conn, target)
        return real_score_job(job, resume_text, provider, conn, settings_arg)

    return _scoring


def test_a_source_gives_way_when_another_is_promoted_mid_slice(
    tmp_path, settings, monkeypatch
):
    """The scenario this exists for: workday is hours into a run and you want
    something else now."""
    provider = FakeProvider([_score_json(score=85)] * 6)
    _two_slice_setup(tmp_path, monkeypatch, provider)
    monkeypatch.setattr(
        pipeline, "score_job", _reorder_after_first_score(settings, ats_control.run_next, "lever")
    )

    result = run_poll(settings)

    assert result.preempted == ["greenhouse"]
    # Both still finish — giving way is a reorder, not a cancellation.
    assert result.slices_processed == 2
    with connection(settings.db_path) as conn:
        assert get_slice_state(conn, "greenhouse") is not None
        assert get_slice_state(conn, "lever") is not None


def test_giving_way_never_repeats_llm_spend(tmp_path, settings, monkeypatch):
    """What makes preemption safe to offer at all. Each slice has 2 scoreable
    jobs; greenhouse gives way after its first, so the total is 4 calls, not
    5 — the job scored before the yield is committed and deduped out."""
    provider = FakeProvider([_score_json(score=85)] * 8)
    _two_slice_setup(tmp_path, monkeypatch, provider)
    monkeypatch.setattr(
        pipeline, "score_job", _reorder_after_first_score(settings, ats_control.run_next, "lever")
    )

    result = run_poll(settings)

    assert provider.calls == 4
    assert result.scored_count == 4


def test_a_source_that_gave_way_keeps_what_it_already_scored(
    tmp_path, settings, monkeypatch
):
    provider = FakeProvider([_score_json(score=85)] * 8)
    _two_slice_setup(tmp_path, monkeypatch, provider)
    monkeypatch.setattr(
        pipeline, "score_job", _reorder_after_first_score(settings, ats_control.run_next, "lever")
    )

    run_poll(settings)

    with connection(settings.db_path) as conn:
        scored = {
            row["global_id"]
            for row in conn.execute("SELECT global_id FROM jobs WHERE status = 'scored'")
        }
    assert {"greenhouse:1", "greenhouse:5", "lever:1", "lever:5"} == scored


def test_a_source_held_mid_slice_stops_and_stays_outstanding(
    tmp_path, settings, monkeypatch
):
    provider = FakeProvider([_score_json(score=85)] * 6)
    _two_slice_setup(tmp_path, monkeypatch, provider)
    monkeypatch.setattr(
        pipeline, "score_job", _reorder_after_first_score(settings, ats_control.hold, "greenhouse")
    )

    result = run_poll(settings)

    assert result.held == ["greenhouse"]
    assert result.preempted == ["greenhouse"]
    with connection(settings.db_path) as conn:
        assert get_slice_state(conn, "greenhouse") is None   # comes back when released
        assert get_slice_state(conn, "lever") is not None


def test_a_source_promoted_mid_run_is_picked_up_even_if_the_run_never_queued_it(
    tmp_path, settings, monkeypatch
):
    """The queue is re-derived from the database, not fixed at run start.

    Without that, "run lever instead" would do nothing whenever lever happened
    to be up to date when the run began — which is the normal case, since most
    sources usually are."""
    provider = FakeProvider([_score_json(score=85)] * 8)
    _two_slice_setup(tmp_path, monkeypatch, provider)

    with connection(settings.db_path) as conn:
        init_schema(conn)
        # lever is up to date, so changed_slices excludes it from this run.
        set_slice_state(
            conn,
            SliceState(
                ats_type="lever",
                last_sha256="a" * 64,
                last_processed_at=datetime.now(UTC),
                row_count=5,
            ),
        )

    def _rerun_lever(conn, _target):
        clear_slice_state(conn, "lever")
        ats_control.run_next(conn, "lever")

    monkeypatch.setattr(
        pipeline, "score_job", _reorder_after_first_score(settings, _rerun_lever, "lever")
    )

    result = run_poll(settings)

    assert "greenhouse" in result.preempted
    assert result.slices_processed == 2      # lever joined the run mid-flight
    with connection(settings.db_path) as conn:
        assert get_slice_state(conn, "lever") is not None
        assert get_slice_state(conn, "greenhouse") is not None


def test_a_failed_source_is_not_retried_forever(tmp_path, settings, monkeypatch):
    """The queue is re-derived from slice_state, and a failed slice leaves
    none — so without an explicit guard it would come back round every pass."""
    provider = FakeProvider([_score_json(score=85)] * 4)
    _two_slice_setup(tmp_path, monkeypatch, provider)

    calls: list[str] = []
    real_download = pipeline.download_slice

    def _download(slice_info, data_dir, conn):
        calls.append(slice_info.ats_type)
        if slice_info.ats_type == "greenhouse":
            raise DownloadError("simulated failure")
        return real_download(slice_info, data_dir, conn)

    monkeypatch.setattr(pipeline, "download_slice", _download)

    result = run_poll(settings)

    assert calls.count("greenhouse") == 1
    assert result.slices_processed == 1


# --- _best_link ------------------------------------------------------------


def _raw(**overrides) -> RawJob:
    kwargs = dict(
        global_id="job-1",
        company="Acme",
        title="Software Engineer",
        url="https://boards.example.com/acme/jobs/1",
        ats_type="greenhouse",
    )
    kwargs.update(overrides)
    return RawJob(**kwargs)


def test_best_link_prefers_apply_url():
    job = _raw(apply_url="https://acme.com/apply/1")
    assert pipeline._best_link(job) == "https://acme.com/apply/1"


def test_best_link_falls_back_to_url_when_apply_url_missing():
    """Every amazon row has apply_url as NaN — the digest showed an empty
    Apply column for the whole slice before this fallback existed."""
    assert pipeline._best_link(_raw(apply_url=None)) == "https://boards.example.com/acme/jobs/1"


def test_best_link_skips_ycombinator_login_wall():
    """YC's apply_url is a sign-in form that shows nothing about the job.
    The public posting page in `url` wins (scope.md §3.1.1)."""
    job = _raw(
        ats_type="ycombinator",
        apply_url=(
            "https://account.ycombinator.com/authenticate?continue="
            "https%3A%2F%2Fwww.workatastartup.com%2Fapplication%3Fsignup_job_id%3D73622"
            "&defaults%5BsignUpActive%5D=true&defaults%5Bwaas_company%5D=26812"
        ),
        url="https://www.ycombinator.com/companies/hype/jobs/Aabj9TY-software-engineer",
    )
    assert (
        pipeline._best_link(job)
        == "https://www.ycombinator.com/companies/hype/jobs/Aabj9TY-software-engineer"
    )


def test_best_link_keeps_apply_url_that_merely_mentions_login():
    """The wall is matched by exact host+path, not by the word 'login'
    appearing somewhere in a perfectly good application link."""
    job = _raw(apply_url="https://jobs.acme.com/apply?redirect=/login&id=7")
    assert pipeline._best_link(job) == "https://jobs.acme.com/apply?redirect=/login&id=7"
