from __future__ import annotations

import smtplib
import time
from datetime import date, datetime

import pytest

from coldstart.db import connection, init_schema
from coldstart.digest import build_digest_html, build_digest_sections, send_digest
from coldstart.models import (
    EligibilityFlag,
    JobRecord,
    JobStatus,
    LocationFlag,
    ResumeId,
)
from coldstart.settings import Settings


def _job(**overrides) -> JobRecord:
    defaults = dict(
        global_id="job-1",
        company="Acme",
        title="Senior Engineer",
        ats_type="greenhouse",
        status=JobStatus.SCORED,
        location_flag=LocationFlag.ACCEPTED,
        eligibility_flag=EligibilityFlag.PASSED,
        first_seen_at=datetime(2026, 8, 19, 12, 0, 0),
    )
    defaults.update(overrides)
    return JobRecord(**defaults)


def _settings(**overrides) -> Settings:
    kwargs = dict(
        experience_years=3.5,
        smtp_user="user@example.com",
        smtp_app_password="app-pw",
        digest_recipient="recipient@example.com",
    )
    kwargs.update(overrides)
    return Settings(**kwargs)


@pytest.fixture
def conn(tmp_path):
    with connection(tmp_path / "test.sqlite3") as c:
        init_schema(c)
        yield c


def _sections(**overrides):
    defaults = dict(
        jobs=[],
        settings=_settings(),
        fetched_count=100,
        filtered_count=40,
        scored_count=10,
        failed_count=1,
        spend_today_usd=0.42,
        providers_used=["deepseek"],
        csv_path="output/scored_2026-08-19.csv",
        unresolved_errors_count=2,
    )
    defaults.update(overrides)
    return build_digest_sections(**defaults)


# --- build_digest_sections -----------------------------------------------------


def test_categorizes_by_score_against_settings_thresholds():
    settings = _settings(score_threshold_strong=70, score_threshold_consider=60)
    jobs = [
        _job(global_id="a", score=90),
        _job(global_id="b", score=65),
        _job(global_id="c", score=30),
    ]
    sections = build_digest_sections(
        jobs,
        settings,
        fetched_count=0,
        filtered_count=0,
        scored_count=3,
        failed_count=0,
        spend_today_usd=0.0,
        providers_used=[],
        csv_path="x.csv",
        unresolved_errors_count=0,
    )
    assert [j.global_id for j in sections.strong] == ["a"]
    assert [j.global_id for j in sections.consider] == ["b"]
    assert sections.strong[0].global_id not in [j.global_id for j in sections.consider]


def test_custom_thresholds_respected_over_llms_own_score_band():
    # A job the LLM itself labeled "consider" should still land in "strong"
    # here once the operator lowers score_threshold_strong below its score —
    # score_band is never trusted for section membership (see digest.py).
    settings = _settings(score_threshold_strong=50, score_threshold_consider=30)
    job = _job(score=55)
    sections = build_digest_sections(
        [job],
        settings,
        fetched_count=0,
        filtered_count=0,
        scored_count=1,
        failed_count=0,
        spend_today_usd=0.0,
        providers_used=[],
        csv_path="x.csv",
        unresolved_errors_count=0,
    )
    assert len(sections.strong) == 1
    assert len(sections.consider) == 0


def test_only_scored_status_jobs_considered_for_score_bands():
    jobs = [
        _job(global_id="pending", status=JobStatus.PENDING, score=None),
        _job(global_id="excluded", status=JobStatus.EXCLUDED, score=None),
        _job(global_id="failed", status=JobStatus.FAILED, score=None),
        _job(global_id="scored", status=JobStatus.SCORED, score=80),
    ]
    sections = build_digest_sections(
        jobs,
        _settings(),
        fetched_count=0,
        filtered_count=0,
        scored_count=1,
        failed_count=0,
        spend_today_usd=0.0,
        providers_used=[],
        csv_path="x.csv",
        unresolved_errors_count=0,
    )
    assert [j.global_id for j in sections.strong] == ["scored"]


def test_uncertain_flags_included_regardless_of_score():
    jobs = [
        _job(
            global_id="low-loc-uncertain",
            score=20,
            location_flag=LocationFlag.UNCERTAIN,
        ),
        _job(
            global_id="high-elig-uncertain",
            score=95,
            eligibility_flag=EligibilityFlag.UNCERTAIN,
        ),
    ]
    sections = build_digest_sections(
        jobs,
        _settings(),
        fetched_count=0,
        filtered_count=0,
        scored_count=2,
        failed_count=0,
        spend_today_usd=0.0,
        providers_used=[],
        csv_path="x.csv",
        unresolved_errors_count=0,
    )
    assert [j.global_id for j in sections.location_uncertain] == ["low-loc-uncertain"]
    assert [j.global_id for j in sections.eligibility_uncertain] == ["high-elig-uncertain"]
    # also still bucketed by score, independent of the uncertain sections
    assert [j.global_id for j in sections.strong] == ["high-elig-uncertain"]


# --- build_digest_html -----------------------------------------------------------


def test_empty_sections_render_no_matches_note():
    sections = _sections()
    out = build_digest_html(sections, date(2026, 8, 19))
    assert "No new matches today" in out
    assert "<table" not in out


def test_populated_sections_all_present():
    jobs = [_job(global_id="strong-1", score=90)]
    sections = _sections(jobs=jobs, scored_count=1)
    out = build_digest_html(sections, date(2026, 8, 19))
    assert "Strong matches" in out
    assert "Worth considering" not in out  # empty section omitted
    assert "Location uncertain" not in out
    assert "Eligibility uncertain" not in out


def test_all_four_sections_populated_and_ordered():
    jobs = [
        _job(global_id="strong-1", score=90),
        _job(global_id="consider-1", score=65),
        _job(
            global_id="loc-uncertain-1",
            score=20,
            location_flag=LocationFlag.UNCERTAIN,
        ),
        _job(
            global_id="elig-uncertain-1",
            score=20,
            eligibility_flag=EligibilityFlag.UNCERTAIN,
        ),
    ]
    sections = _sections(jobs=jobs, scored_count=4)
    out = build_digest_html(sections, date(2026, 8, 19))

    strong_idx = out.index("Strong matches")
    consider_idx = out.index("Worth considering")
    loc_idx = out.index("Location uncertain")
    elig_idx = out.index("Eligibility uncertain")
    assert strong_idx < consider_idx < loc_idx < elig_idx


def test_score_descending_within_section():
    jobs = [
        _job(global_id="low", score=71),
        _job(global_id="high", score=99),
        _job(global_id="mid", score=80),
    ]
    sections = _sections(jobs=jobs, scored_count=3)
    out = build_digest_html(sections, date(2026, 8, 19))

    strong_html = out[out.index("Strong matches") :]
    assert strong_html.index(">99<") < strong_html.index(">80<") < strong_html.index(">71<")


def test_company_and_title_html_escaped():
    job = _job(
        company='<script>alert("x")</script>',
        title="R&D <Engineer>",
        score=90,
    )
    sections = _sections(jobs=[job], scored_count=1)
    out = build_digest_html(sections, date(2026, 8, 19))

    assert "<script>alert" not in out
    assert "&lt;script&gt;" in out
    assert "R&amp;D &lt;Engineer&gt;" in out


def test_footer_contains_counts_spend_providers_csv_errors():
    sections = _sections(
        fetched_count=500,
        filtered_count=120,
        scored_count=30,
        failed_count=2,
        spend_today_usd=1.2345,
        providers_used=["deepseek", "kimi"],
        csv_path="output/scored_2026-08-19.csv",
        unresolved_errors_count=3,
    )
    out = build_digest_html(sections, date(2026, 8, 19))

    assert "500" in out
    assert "120" in out
    assert "30" in out
    assert "$1.23" in out
    assert "deepseek, kimi" in out
    assert "output/scored_2026-08-19.csv" in out
    assert "Unresolved errors: 3" in out


def test_apply_link_rendered_when_present():
    job = _job(score=90, apply_url="https://example.com/apply?id=1")
    sections = _sections(jobs=[job], scored_count=1)
    out = build_digest_html(sections, date(2026, 8, 19))
    assert 'href="https://example.com/apply?id=1"' in out


def test_resume_used_and_skills_rendered():
    job = _job(
        score=90,
        resume_used=ResumeId.C,
        matched_skills=["Python", "AWS"],
        missing_skills=["Go"],
    )
    sections = _sections(jobs=[job], scored_count=1)
    out = build_digest_html(sections, date(2026, 8, 19))
    assert ">C<" in out
    assert "Python; AWS" in out
    assert ">Go<" in out


# --- send_digest -----------------------------------------------------------------


def test_send_digest_success_logs_sent(conn, mocker):
    mock_smtp_cls = mocker.patch("coldstart.digest.smtplib.SMTP")
    mock_smtp = mock_smtp_cls.return_value.__enter__.return_value

    result = send_digest(_settings(), "<html>x</html>", "Subject line", 5, conn)

    assert result is True
    mock_smtp.starttls.assert_called_once()
    mock_smtp.login.assert_called_once_with("user@example.com", "app-pw")
    mock_smtp.send_message.assert_called_once()

    row = conn.execute("SELECT status, job_count, error FROM email_log").fetchone()
    assert row["status"] == "sent"
    assert row["job_count"] == 5
    assert row["error"] is None


def test_send_digest_message_has_plaintext_and_html_parts(conn, mocker):
    mock_smtp_cls = mocker.patch("coldstart.digest.smtplib.SMTP")
    mock_smtp = mock_smtp_cls.return_value.__enter__.return_value

    html_body = "<html><body><h1>Digest</h1><p>Acme Corp</p></body></html>"
    send_digest(_settings(), html_body, "Subject", 1, conn)

    message = mock_smtp.send_message.call_args[0][0]
    assert message.is_multipart()
    content_types = {part.get_content_type() for part in message.walk()}
    assert "text/plain" in content_types
    assert "text/html" in content_types

    plain_part = next(p for p in message.walk() if p.get_content_type() == "text/plain")
    assert "Acme Corp" in plain_part.get_content()
    html_part = next(p for p in message.walk() if p.get_content_type() == "text/html")
    assert "<h1>Digest</h1>" in html_part.get_content()


def test_send_digest_failure_retries_then_logs_failed(conn, mocker, monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    mock_smtp_cls = mocker.patch("coldstart.digest.smtplib.SMTP")
    mock_smtp = mock_smtp_cls.return_value.__enter__.return_value
    mock_smtp.send_message.side_effect = smtplib.SMTPException("connection refused")

    result = send_digest(_settings(), "<html>x</html>", "Subject", 3, conn)

    assert result is False
    assert mock_smtp.send_message.call_count == 3

    email_row = conn.execute("SELECT status, error FROM email_log").fetchone()
    assert email_row["status"] == "failed"
    assert "connection refused" in email_row["error"]

    error_row = conn.execute("SELECT stage FROM errors").fetchone()
    assert error_row["stage"] == "email"


def test_send_digest_succeeds_after_transient_failure(conn, mocker, monkeypatch):
    monkeypatch.setattr(time, "sleep", lambda s: None)
    mock_smtp_cls = mocker.patch("coldstart.digest.smtplib.SMTP")
    mock_smtp = mock_smtp_cls.return_value.__enter__.return_value
    mock_smtp.send_message.side_effect = [
        smtplib.SMTPException("transient"),
        None,
    ]

    result = send_digest(_settings(), "<html>x</html>", "Subject", 2, conn)

    assert result is True
    assert mock_smtp.send_message.call_count == 2
    row = conn.execute("SELECT status FROM email_log").fetchone()
    assert row["status"] == "sent"


def test_send_digest_empty_digest_still_sends(conn, mocker):
    mock_smtp_cls = mocker.patch("coldstart.digest.smtplib.SMTP")
    mock_smtp = mock_smtp_cls.return_value.__enter__.return_value

    sections = _sections()
    html_body = build_digest_html(sections, date(2026, 8, 19))
    assert "No new matches today" in html_body

    result = send_digest(_settings(), html_body, "No matches today", 0, conn)

    assert result is True
    mock_smtp.send_message.assert_called_once()
    row = conn.execute("SELECT status, job_count FROM email_log").fetchone()
    assert row["status"] == "sent"
    assert row["job_count"] == 0
