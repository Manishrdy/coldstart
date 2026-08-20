from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest
from conftest import FakeProvider
from freezegun import freeze_time

import coldstart.pipeline as pipeline
from coldstart.db import connection, get_slice_state, init_schema, set_slice_state
from coldstart.fetcher import DownloadError
from coldstart.models import ResumeId, SliceState
from coldstart.pipeline import run_digest, run_poll
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

    assert result.fetched_count == 5
    assert result.filtered_count == 2  # survives title+location+eligibility: jobs 1 and 5
    assert result.scored_count == 2
    assert result.failed_count == 0
    assert result.excluded_count == 1  # job 4 (security clearance)
    assert result.slices_processed == 1
    assert result.csv_path is not None
    assert Path(result.csv_path).exists()

    with connection(settings.db_path) as conn:
        rows = conn.execute("SELECT global_id, status, score FROM jobs").fetchall()
        state = get_slice_state(conn, "greenhouse")

    by_id = {r["global_id"]: r for r in rows}
    assert by_id["greenhouse:1"]["status"] == "scored"
    assert by_id["greenhouse:1"]["score"] == 85
    assert by_id["greenhouse:4"]["status"] == "excluded"
    assert by_id["greenhouse:5"]["status"] == "scored"
    assert "greenhouse:2" not in by_id  # title-rejected, never persisted
    assert "greenhouse:3" not in by_id  # location-rejected, never persisted

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
    assert all(row["status"] in {"scored", "excluded", "failed"} for row in rows)


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
    assert "Fetched: 5" in html_content
    assert "Filtered: 2" in html_content
    assert "Scored: 2" in html_content
    assert "fake" in html_content  # provider name, from spend_log

    with connection(settings.db_path) as conn:
        email_row = conn.execute("SELECT status, job_count FROM email_log").fetchone()
    assert email_row["status"] == "sent"
    assert email_row["job_count"] == 2


def test_blocked_company_is_never_scored_persisted_or_sent_to_an_llm(
    settings, tmp_path, monkeypatch
):
    """The hard guarantee for the block list (filters/company.py).

    excluded_ats.json stops these employers' own feeds from being downloaded,
    but not the same employer posting through someone else's platform —
    observed live as `amazon.jobs.personio.com` on the personio slice, and as
    `uberfreight`/`googlefiber` on greenhouse, 8 of whose postings survive the
    title filter. This asserts the second gate holds end to end."""
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
            ats_type="personio",
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
            ats_type="personio",
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
            ats_type="personio",
            description="Build internal tooling.",
            posted_at=_recent_iso(),
            raw=None,
        ),
    ]
    _write_parquet(tmp_path / "personio.parquet", rows)

    provider = FakeProvider([_score_json(80, "strong", "Good fit.")])
    monkeypatch.setattr(pipeline, "build_active_provider", lambda s: provider)
    monkeypatch.setattr(pipeline, "fetch_manifest", lambda url: _manifest(["personio"], "sha-1"))
    monkeypatch.setattr(
        pipeline,
        "download_slice",
        lambda slice_info, data_dir, conn: tmp_path / "personio.parquet",
    )

    result = pipeline.run_poll(settings)

    # Exactly one LLM call: the roofing company. Neither blocked row cost a cent.
    assert provider.calls == 1
    assert result.scored_count == 1

    with connection(settings.db_path) as conn:
        companies = {row[0] for row in conn.execute("SELECT company FROM jobs")}
    assert companies == {"apple-roofing"}


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
