from __future__ import annotations

import smtplib
import time
from datetime import date, datetime

import pytest

from coldstart.db import connection, init_schema
from coldstart.digest import (
    build_digest_html,
    build_digest_sections,
    render_digest_text,
    send_digest,
)
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


def test_categorizes_by_score_against_settings_threshold():
    settings = _settings(score_threshold_strong=70)
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
    # Binary: only "a" clears the bar. "b" and "c" both fall below it and
    # land nowhere — there is no middle "consider" section any more.
    assert [j.global_id for j in sections.strong] == ["a"]


def test_custom_threshold_respected_over_llms_own_score_band():
    # A job the LLM itself labeled "consider" should still land in "strong"
    # here once the operator lowers score_threshold_strong below its score —
    # score_band is never trusted for section membership (see digest.py).
    settings = _settings(score_threshold_strong=50)
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
            status=JobStatus.EXCLUDED_LOCATION,
            location_flag=LocationFlag.UNCERTAIN,
            location_reason="unresolved",
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
    # No job content of any kind — no cards, no sections, no apply buttons.
    assert "Apply" not in out
    assert "Strong matches" not in out


def test_populated_sections_all_present():
    jobs = [_job(global_id="strong-1", score=90)]
    sections = _sections(jobs=jobs, scored_count=1)
    out = build_digest_html(sections, date(2026, 8, 19))
    assert "Strong matches" in out
    assert "Location uncertain" not in out
    assert "Eligibility uncertain" not in out


def test_remaining_sections_populated_and_ordered():
    jobs = [
        _job(global_id="strong-1", score=90),
        _job(
            global_id="loc-uncertain-1",
            status=JobStatus.EXCLUDED_LOCATION,
            location_flag=LocationFlag.UNCERTAIN,
            location_reason="bare_remote",
        ),
        _job(
            global_id="elig-uncertain-1",
            score=20,
            eligibility_flag=EligibilityFlag.UNCERTAIN,
        ),
    ]
    sections = _sections(jobs=jobs, scored_count=3)
    out = build_digest_html(sections, date(2026, 8, 19))

    strong_idx = out.index("Strong matches")
    loc_idx = out.index("Held back by the location filter")
    elig_idx = out.index("Eligibility uncertain")
    assert strong_idx < loc_idx < elig_idx


def test_score_descending_within_section():
    jobs = [
        _job(global_id="low", score=81),
        _job(global_id="high", score=99),
        _job(global_id="mid", score=90),
    ]
    sections = _sections(jobs=jobs, scored_count=3)
    out = build_digest_html(sections, date(2026, 8, 19))

    strong_html = out[out.index("Strong matches") :]
    assert strong_html.index(">99<") < strong_html.index(">90<") < strong_html.index(">81<")


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
    assert "Resume C" in out
    # Matched skills render as individual chips rather than a joined string.
    assert ">Python<" in out
    assert ">AWS<" in out
    # Missing skills are labelled as gaps and visually de-emphasised.
    assert "Gaps" in out
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


# --- email template (professional HTML) ------------------------------------


def test_apply_button_links_to_the_apply_url():
    job = _job(score=90, apply_url="https://example.com/apply?id=1&x=2")
    out = build_digest_html(_sections(jobs=[job], scored_count=1), date(2026, 8, 19))
    assert 'href="https://example.com/apply?id=1&amp;x=2"' in out
    assert "Apply" in out


def test_a_job_without_a_link_says_so_instead_of_rendering_an_empty_cell():
    """Real-data case: every one of amazon's rows has apply_url as NaN. An
    empty cell reads as a rendering bug; saying so reads as information."""
    job = _job(score=90, apply_url=None)
    out = build_digest_html(_sections(jobs=[job], scored_count=1), date(2026, 8, 19))
    assert "No application link published" in out
    assert "<a href" not in out


def test_the_email_is_a_table_based_600px_shell():
    """Email clients don't reliably support flexbox, grid, or stylesheets."""
    out = build_digest_html(_sections(jobs=[_job(score=90)], scored_count=1), date(2026, 8, 19))
    assert "max-width:600px" in out
    assert 'role="presentation"' in out
    assert "display:flex" not in out
    assert "display:grid" not in out
    assert "<link" not in out
    # No remote assets — mail clients block them by default.
    assert "<img" not in out


def test_a_hidden_preheader_summarises_the_email():
    sections = _sections(jobs=[_job(score=90), _job(global_id="b", score=65)], scored_count=2)
    out = build_digest_html(sections, date(2026, 8, 19))
    assert "1 strong match(es)" in out
    assert "display:none" in out


def test_the_summary_band_shows_the_top_score():
    jobs = [_job(global_id="a", score=91), _job(global_id="b", score=77)]
    out = build_digest_html(_sections(jobs=jobs, scored_count=2), date(2026, 8, 19))
    assert "Top score" in out
    assert ">91<" in out


def test_scores_still_order_descending_within_a_section():
    jobs = [_job(global_id=str(n), score=n) for n in (80, 99, 81)]
    out = build_digest_html(_sections(jobs=jobs, scored_count=3), date(2026, 8, 19))
    assert out.index(">99<") < out.index(">81<") < out.index(">80<")


def test_html_escaping_survives_the_redesign():
    job = _job(
        score=90,
        company="<script>alert(1)</script>",
        title="R&D <Engineer>",
        reasoning="5 > 3 & 2 < 4",
    )
    out = build_digest_html(_sections(jobs=[job], scored_count=1), date(2026, 8, 19))
    assert "<script>alert" not in out
    assert "&lt;script&gt;" in out
    assert "R&amp;D &lt;Engineer&gt;" in out


# --- plain-text alternative ------------------------------------------------


def test_plain_text_alternative_is_written_not_scraped():
    """A regex tag-strip over a card layout produces soup. Some clients show
    this part, so it has to stand on its own."""
    job = _job(
        score=90,
        company="Acme Corp",
        title="Senior Software Engineer",
        location="Austin, TX",
        apply_url="https://example.com/apply",
        matched_skills=["Python", "AWS"],
        missing_skills=["Go"],
        reasoning="Strong backend overlap.",
    )
    text = render_digest_text(_sections(jobs=[job], scored_count=1), date(2026, 8, 19))

    assert "STRONG MATCHES (1)" in text
    assert "[90] Senior Software Engineer" in text
    assert "Acme Corp — Austin, TX" in text
    assert "Matched: Python; AWS" in text
    assert "Gaps: Go" in text
    assert "Apply: https://example.com/apply" in text
    assert "<" not in text and ">" not in text.replace("—", "")


def test_plain_text_says_when_a_job_has_no_link():
    job = _job(score=90, apply_url=None)
    text = render_digest_text(_sections(jobs=[job], scored_count=1), date(2026, 8, 19))
    assert "Apply: (no link published)" in text


def test_plain_text_empty_digest():
    text = render_digest_text(_sections(), date(2026, 8, 19))
    assert "No new matches today." in text


def test_send_digest_uses_the_supplied_text_body(conn, mocker):
    mock_smtp_cls = mocker.patch("coldstart.digest.smtplib.SMTP")
    mock_smtp = mock_smtp_cls.return_value.__enter__.return_value

    send_digest(
        _settings(), "<html><body>ignored</body></html>", "Subject", 1, conn,
        text_body="hand written plain text",
    )
    message = mock_smtp.send_message.call_args[0][0]
    plain = next(p for p in message.walk() if p.get_content_type() == "text/plain")
    assert "hand written plain text" in plain.get_content()


def test_nothing_sets_an_unbreakable_width_floor():
    """Mobile regression. A table can't shrink below its widest unbreakable
    content: `white-space:nowrap` chips set a ~350px floor and the
    `width="600"` attribute acts as a minimum in some engines. Together they
    pushed the email past a phone viewport and clipped the right edge."""
    job = _job(
        score=90,
        title="Senior Staff Software Engineer, Platform Infrastructure and Reliability",
        matched_skills=["Distributed systems and event-driven architecture at scale"],
    )
    out = build_digest_html(_sections(jobs=[job], scored_count=1), date(2026, 8, 19))
    assert "white-space:nowrap" not in out
    assert 'width="600"' not in out
    # Long titles and reasoning must be able to break rather than force a floor.
    assert "word-break:break-word" in out
