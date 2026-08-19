import logging

import pytest
from freezegun import freeze_time

from coldstart.budget import (
    BudgetExceeded,
    check_budget,
    record_spend,
    reset_budget_warning_state,
    today_spend,
)
from coldstart.db import connection, init_schema
from coldstart.scoring.base import LLMUsage
from coldstart.settings import Settings


@pytest.fixture(autouse=True)
def _reset_warning_state():
    reset_budget_warning_state()
    yield
    reset_budget_warning_state()


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
        daily_token_spend_ceiling_usd=3.0,
        timezone="America/Los_Angeles",
    )
    kwargs.update(overrides)
    return Settings(**kwargs)


def _usage(input_tokens=100, cached_tokens=0, output_tokens=10) -> LLMUsage:
    return LLMUsage(
        input_tokens=input_tokens, cached_tokens=cached_tokens, output_tokens=output_tokens
    )


# --- record_spend / today_spend -----------------------------------------------


def test_record_spend_writes_row(conn):
    record_spend(conn, "deepseek", "deepseek-chat", _usage(), cost=0.05, job_ref="job-1")
    row = conn.execute("SELECT provider, model, est_cost_usd, job_ref FROM spend_log").fetchone()
    assert row["provider"] == "deepseek"
    assert row["model"] == "deepseek-chat"
    assert row["est_cost_usd"] == pytest.approx(0.05)
    assert row["job_ref"] == "job-1"


def test_today_spend_accumulates_multiple_records(conn):
    record_spend(conn, "deepseek", "deepseek-chat", _usage(), cost=0.5, job_ref="a")
    record_spend(conn, "deepseek", "deepseek-chat", _usage(), cost=0.25, job_ref="b")
    record_spend(conn, "kimi", "moonshot-v1-8k", _usage(), cost=0.1, job_ref="c")
    assert today_spend(conn, "America/Los_Angeles") == pytest.approx(0.85)


def test_today_spend_zero_when_no_records(conn):
    assert today_spend(conn, "America/Los_Angeles") == 0.0


def test_today_spend_respects_local_timezone_not_utc(conn):
    # 2026-08-20T06:00Z = 23:00 on 2026-08-19 in LA (PDT, UTC-7) — still "today" = Aug 19.
    with freeze_time("2026-08-20T06:00:00+00:00"):
        record_spend(conn, "deepseek", "deepseek-chat", _usage(), cost=1.0, job_ref="late-night")
        assert today_spend(conn, "America/Los_Angeles") == pytest.approx(1.0)

    # 2026-08-20T08:00Z = 01:00 on 2026-08-20 in LA — a new LA day has begun;
    # the earlier record belongs to yesterday's (LA) bucket, not today's.
    with freeze_time("2026-08-20T08:00:00+00:00"):
        assert today_spend(conn, "America/Los_Angeles") == pytest.approx(0.0)


# --- check_budget ---------------------------------------------------------------


def test_check_budget_passes_when_under_ceiling(conn):
    settings = _settings(daily_token_spend_ceiling_usd=3.0)
    record_spend(conn, "deepseek", "deepseek-chat", _usage(), cost=1.0, job_ref="a")
    check_budget(conn, settings)  # should not raise


def test_check_budget_raises_at_exact_ceiling(conn):
    settings = _settings(daily_token_spend_ceiling_usd=3.0)
    record_spend(conn, "deepseek", "deepseek-chat", _usage(), cost=3.0, job_ref="a")
    with pytest.raises(BudgetExceeded):
        check_budget(conn, settings)


def test_check_budget_raises_over_ceiling(conn):
    settings = _settings(daily_token_spend_ceiling_usd=3.0)
    record_spend(conn, "deepseek", "deepseek-chat", _usage(), cost=5.0, job_ref="a")
    with pytest.raises(BudgetExceeded):
        check_budget(conn, settings)


def test_check_budget_logs_critical_on_breach(conn, caplog):
    settings = _settings(daily_token_spend_ceiling_usd=3.0)
    record_spend(conn, "deepseek", "deepseek-chat", _usage(), cost=3.0, job_ref="a")
    with caplog.at_level(logging.CRITICAL, logger="coldstart.budget"):
        with pytest.raises(BudgetExceeded):
            check_budget(conn, settings)
    assert any("ceiling breached" in r.getMessage() for r in caplog.records)


def test_check_budget_warns_once_at_80_percent(conn, caplog):
    settings = _settings(daily_token_spend_ceiling_usd=3.0)
    record_spend(conn, "deepseek", "deepseek-chat", _usage(), cost=2.5, job_ref="a")  # 83%
    with caplog.at_level(logging.WARNING, logger="coldstart.budget"):
        check_budget(conn, settings)
        check_budget(conn, settings)
        check_budget(conn, settings)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1


def test_check_budget_no_warning_under_80_percent(conn, caplog):
    settings = _settings(daily_token_spend_ceiling_usd=3.0)
    record_spend(conn, "deepseek", "deepseek-chat", _usage(), cost=1.0, job_ref="a")  # 33%
    with caplog.at_level(logging.WARNING, logger="coldstart.budget"):
        check_budget(conn, settings)
    assert not any(r.levelno == logging.WARNING for r in caplog.records)


def test_check_budget_warning_resets_on_new_day(conn, caplog):
    settings = _settings(daily_token_spend_ceiling_usd=3.0)
    with freeze_time("2026-08-19T20:00:00+00:00"):
        record_spend(conn, "deepseek", "deepseek-chat", _usage(), cost=2.5, job_ref="a")
        with caplog.at_level(logging.WARNING, logger="coldstart.budget"):
            check_budget(conn, settings)
        assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1

    caplog.clear()
    with freeze_time("2026-08-20T20:00:00+00:00"):
        record_spend(conn, "deepseek", "deepseek-chat", _usage(), cost=2.5, job_ref="b")
        with caplog.at_level(logging.WARNING, logger="coldstart.budget"):
            check_budget(conn, settings)
        assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1
