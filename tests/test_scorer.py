import json
import logging
import time as time_module

import pytest
from conftest import FakeProvider

from coldstart.budget import BudgetExceeded, record_spend
from coldstart.db import connection, init_schema
from coldstart.models import RawJob, ScoreBand
from coldstart.scoring.base import LLMUsage, RateLimitError
from coldstart.scoring.scorer import score_job, score_jobs
from coldstart.settings import Settings

_RESUME_TEXT = "WORK EXPERIENCE\nAcme\n• Built things with Python.\nSKILLS\nPython\n"


@pytest.fixture(autouse=True)
def _fast_retries(monkeypatch):
    monkeypatch.setattr(time_module, "sleep", lambda s: None)


@pytest.fixture
def conn(tmp_path):
    with connection(tmp_path / "test.sqlite3") as c:
        init_schema(c)
        yield c


def _settings(**overrides) -> Settings:
    kwargs = dict(
        experience_years=3.5,
        smtp_user="user@example.com",
        smtp_app_password="app-pw",
        digest_recipient="user@example.com",
        daily_token_spend_ceiling_usd=100.0,
        max_retries_per_provider=2,
    )
    kwargs.update(overrides)
    return Settings(**kwargs)


def _job(**overrides) -> RawJob:
    kwargs = dict(
        global_id="job-1",
        company="Acme",
        title="AI Engineer",
        url="https://x",
        ats_type="greenhouse",
        description="Build agentic pipelines.",
    )
    kwargs.update(overrides)
    return RawJob(**kwargs)


def _score_dict(**overrides) -> dict:
    base = dict(
        eligible=True,
        disqualification_reason=None,
        score=80,
        score_band="strong",
        tech_stack_match=85,
        seniority_fit=75,
        experience_fit=80,
        role_type_fit=70,
        matched_skills=["python"],
        missing_skills=["go"],
        reasoning="Strong overlap.",
    )
    base.update(overrides)
    return base


VALID_SCORE_JSON = json.dumps(_score_dict())


# --- score_job: happy path / parsing --------------------------------------------


def test_score_job_happy_path(conn):
    provider = FakeProvider([VALID_SCORE_JSON])
    score = score_job(_job(), _RESUME_TEXT, provider, conn, _settings())
    assert score is not None
    assert score.score == 80
    assert score.score_band == ScoreBand.STRONG
    assert provider.calls == 1


def test_score_job_parses_markdown_fenced_json(conn):
    fenced = f"```json\n{VALID_SCORE_JSON}\n```"
    provider = FakeProvider([fenced])
    score = score_job(_job(), _RESUME_TEXT, provider, conn, _settings())
    assert score is not None
    assert score.score == 80


def test_score_job_records_spend_on_success(conn):
    provider = FakeProvider([VALID_SCORE_JSON], name="deepseek")
    score_job(_job(), _RESUME_TEXT, provider, conn, _settings())
    row = conn.execute("SELECT provider, job_ref FROM spend_log").fetchone()
    assert row["provider"] == "deepseek"
    assert row["job_ref"] == "job-1"


# --- score_job: schema-correction retry ------------------------------------------


def test_score_job_malformed_json_then_succeeds(conn):
    provider = FakeProvider(["not json", VALID_SCORE_JSON])
    score = score_job(_job(), _RESUME_TEXT, provider, conn, _settings())
    assert score is not None
    assert provider.calls == 2


def test_score_job_malformed_twice_returns_none_and_logs_error(conn, caplog):
    provider = FakeProvider(["not json", "still not json"])
    with caplog.at_level(logging.WARNING, logger="coldstart.scoring.scorer"):
        score = score_job(_job(global_id="job-x"), _RESUME_TEXT, provider, conn, _settings())
    assert score is None
    row = conn.execute("SELECT stage, job_ref, provider FROM errors").fetchone()
    assert row["stage"] == "llm_score"
    assert row["job_ref"] == "job-x"
    assert any("schema-correction retry" in r.getMessage() for r in caplog.records)


def test_score_job_ineligible_zero_score_accepted(conn):
    payload = json.dumps(_score_dict(eligible=False, score=0, score_band="reject"))
    provider = FakeProvider([payload])
    score = score_job(_job(), _RESUME_TEXT, provider, conn, _settings())
    assert score is not None
    assert score.eligible is False
    assert score.score == 0


def test_score_job_ineligible_nonzero_score_triggers_correction_retry(conn):
    invalid = json.dumps(_score_dict(eligible=False, score=50))  # violates JobScore's validator
    provider = FakeProvider([invalid, VALID_SCORE_JSON])
    score = score_job(_job(), _RESUME_TEXT, provider, conn, _settings())
    assert score is not None
    assert provider.calls == 2


# --- score_job: exhausted retries (no cross-provider fallback) --------------------


def test_score_job_exhausted_retries_returns_none_and_logs_error(conn):
    provider = FakeProvider(
        [RateLimitError("429"), RateLimitError("429")], name="p1"
    )
    score = score_job(
        _job(global_id="job-y"), _RESUME_TEXT, provider, conn, _settings(max_retries_per_provider=2)
    )
    assert score is None
    row = conn.execute("SELECT provider FROM errors WHERE job_ref = 'job-y'").fetchone()
    assert row["provider"] == "p1"


# --- score_job: budget -------------------------------------------------------------


def test_score_job_budget_exceeded_stops_before_calling_provider(conn, caplog):
    settings = _settings(daily_token_spend_ceiling_usd=1.0)
    record_spend(
        conn,
        "deepseek",
        "deepseek-chat",
        LLMUsage(input_tokens=1, cached_tokens=0, output_tokens=1),
        cost=1.0,
        job_ref="prior",
    )
    provider = FakeProvider([VALID_SCORE_JSON])
    with caplog.at_level(logging.CRITICAL, logger="coldstart.budget"):
        with pytest.raises(BudgetExceeded):
            score_job(_job(), _RESUME_TEXT, provider, conn, settings)
    assert provider.calls == 0
    assert any("ceiling breached" in r.getMessage() for r in caplog.records)


# --- score_jobs: batching ----------------------------------------------------------


def test_score_jobs_default_batch_size_scores_one_at_a_time(conn):
    provider = FakeProvider([VALID_SCORE_JSON, VALID_SCORE_JSON])
    jobs = [_job(global_id="job-1"), _job(global_id="job-2")]
    results = score_jobs(jobs, _RESUME_TEXT, provider, conn, _settings())
    assert len(results) == 2
    assert provider.calls == 2
    assert [job.global_id for job, _ in results] == ["job-1", "job-2"]


def test_score_jobs_batch_size_2_returns_two_results_in_order(conn):
    batch_response = json.dumps(
        [_score_dict(score=80), _score_dict(score=60, score_band="consider")]
    )
    provider = FakeProvider([batch_response])
    jobs = [_job(global_id="job-1"), _job(global_id="job-2")]
    results = score_jobs(jobs, _RESUME_TEXT, provider, conn, _settings(), batch_size=2)

    assert len(results) == 2
    assert provider.calls == 1
    assert results[0][0].global_id == "job-1"
    assert results[0][1].score == 80
    assert results[1][0].global_id == "job-2"
    assert results[1][1].score == 60


def test_score_jobs_batch_wrong_length_triggers_correction_retry(conn):
    wrong_length = json.dumps([_score_dict()])  # only 1 object for a batch of 2
    batch_response = json.dumps([_score_dict(score=80), _score_dict(score=60)])
    provider = FakeProvider([wrong_length, batch_response])
    jobs = [_job(global_id="job-1"), _job(global_id="job-2")]
    results = score_jobs(jobs, _RESUME_TEXT, provider, conn, _settings(), batch_size=2)
    assert provider.calls == 2
    assert all(score is not None for _, score in results)


def test_score_jobs_batch_schema_exhausted_marks_batch_none_and_logs_error(conn):
    provider = FakeProvider(["not json", "still not json"], name="p1")
    jobs = [_job(global_id="job-1"), _job(global_id="job-2")]
    results = score_jobs(jobs, _RESUME_TEXT, provider, conn, _settings(), batch_size=2)
    assert [score for _, score in results] == [None, None]
    row = conn.execute("SELECT stage, provider FROM errors").fetchone()
    assert row["stage"] == "llm_score"
    assert row["provider"] == "p1"


def test_score_jobs_batch_provider_exhausted_marks_whole_batch_none(conn):
    p1 = FakeProvider([RateLimitError("429"), RateLimitError("429")], name="p1")
    jobs = [_job(global_id="job-1"), _job(global_id="job-2")]
    settings = _settings(max_retries_per_provider=2)
    results = score_jobs(jobs, _RESUME_TEXT, p1, conn, settings, batch_size=2)
    assert [score for _, score in results] == [None, None]


def test_score_jobs_summary_logged(conn, caplog):
    provider = FakeProvider([VALID_SCORE_JSON])
    with caplog.at_level(logging.INFO, logger="coldstart.scoring.scorer"):
        score_jobs([_job()], _RESUME_TEXT, provider, conn, _settings())
    assert any("score_jobs summary" in r.getMessage() for r in caplog.records)
