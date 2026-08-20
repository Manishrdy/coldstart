from __future__ import annotations

import json
from datetime import UTC, date, datetime, timedelta

import pytest

from coldstart.digest import DigestSections, build_digest_html, render_digest_text
from coldstart.email_template import (
    HTML_TEMPLATE,
    TEXT_TEMPLATE,
    TemplateProblem,
    load_theme,
    template_version,
)
from coldstart.email_template import render as render_template
from coldstart.models import (
    EligibilityFlag,
    JobRecord,
    JobStatus,
    LocationFlag,
    ResumeId,
    ScoreBand,
)


def _job(**overrides) -> JobRecord:
    now = datetime.now(UTC)
    base = dict(
        global_id="gh:1",
        requisition_id="REQ-1",
        company="Acme Corp",
        title="Senior Software Engineer",
        location="Austin, TX",
        apply_url="https://example.com/apply",
        ats_type="greenhouse",
        posted_at=now - timedelta(days=3),
        resume_used=ResumeId.A,
        score=90,
        score_band=ScoreBand.STRONG,
        eligible=True,
        matched_skills=["Python", "AWS"],
        missing_skills=["Go"],
        reasoning="Strong backend overlap.",
        status=JobStatus.SCORED,
        location_flag=LocationFlag.ACCEPTED,
        eligibility_flag=EligibilityFlag.PASSED,
        provider_used="deepseek",
        first_seen_at=now,
        scored_at=now,
    )
    base.update(overrides)
    return JobRecord(**base)


def _sections(**overrides) -> DigestSections:
    base = dict(
        fetched_count=100,
        filtered_count=20,
        scored_count=2,
        failed_count=0,
        spend_today_usd=1.23,
        providers_used=["deepseek"],
        csv_path="output/scored.csv",
        unresolved_errors_count=0,
    )
    base.update(overrides)
    return DigestSections(**base)


# --- the template is genuinely in use --------------------------------------


def test_the_shipped_template_renders():
    out = render_template(
        HTML_TEMPLATE, sections=_sections(strong=[_job()]), run_date=date(2026, 8, 20)
    )
    assert "Senior Software Engineer" in out
    assert "Acme Corp" in out


def test_build_digest_html_uses_the_template_not_the_fallback():
    sections = _sections(strong=[_job()])
    assert build_digest_html(sections, date(2026, 8, 20)) == render_template(
        HTML_TEMPLATE, sections=sections, run_date=date(2026, 8, 20)
    )


def test_theme_values_reach_the_rendered_email():
    theme = load_theme()
    out = build_digest_html(_sections(strong=[_job()]), date(2026, 8, 20))
    assert theme["accent"] in out
    assert theme["brand"] in out


# --- a broken template must never cost a digest ----------------------------


def test_a_broken_template_falls_back_instead_of_raising(tmp_path, monkeypatch, caplog):
    import coldstart.email_template as et

    broken = tmp_path / "email"
    broken.mkdir()
    (broken / "theme.json").write_text((et.TEMPLATE_DIR / "theme.json").read_text())
    (broken / HTML_TEMPLATE).write_text("{% if x %}never closed")
    (broken / TEXT_TEMPLATE).write_text("fine")
    monkeypatch.setattr(et, "TEMPLATE_DIR", broken)

    with caplog.at_level("ERROR"):
        out = build_digest_html(_sections(strong=[_job()]), date(2026, 8, 20))

    # The built-in layout, and a loud log — never an exception.
    assert "Senior Software Engineer" in out
    assert "falling back to the built-in layout" in caplog.text


def test_a_missing_template_falls_back(tmp_path, monkeypatch):
    import coldstart.email_template as et

    empty = tmp_path / "email"
    empty.mkdir()
    (empty / "theme.json").write_text((et.TEMPLATE_DIR / "theme.json").read_text())
    monkeypatch.setattr(et, "TEMPLATE_DIR", empty)
    assert "Senior Software Engineer" in build_digest_html(
        _sections(strong=[_job()]), date(2026, 8, 20)
    )


def test_a_broken_theme_falls_back(tmp_path, monkeypatch):
    import coldstart.email_template as et

    bad = tmp_path / "email"
    bad.mkdir()
    (bad / "theme.json").write_text("{not json")
    (bad / HTML_TEMPLATE).write_text("{{ brand }}")
    monkeypatch.setattr(et, "TEMPLATE_DIR", bad)
    with pytest.raises(TemplateProblem):
        load_theme()
    # And the public path still produces an email.
    assert "Senior Software Engineer" in build_digest_html(
        _sections(strong=[_job()]), date(2026, 8, 20)
    )


# --- escaping is decided by us, not by the template author -----------------


def test_html_is_autoescaped():
    job = _job(company="<script>alert(1)</script>", title="R&D <Engineer>")
    out = build_digest_html(_sections(strong=[job]), date(2026, 8, 20))
    assert "<script>alert" not in out
    assert "&lt;script&gt;" in out
    assert "R&amp;D &lt;Engineer&gt;" in out


def test_plain_text_is_not_escaped():
    """Escaping everything leaks `&amp;` and `&#39;` into the text/plain part;
    escaping nothing lets a feed inject markup into the HTML. It has to be
    per-template."""
    job = _job(company="Smith & Sons", reasoning="The role's scope is wide.")
    text = render_digest_text(_sections(strong=[job]), date(2026, 8, 20))
    assert "Smith & Sons" in text
    assert "The role's scope is wide." in text
    assert "&amp;" not in text
    assert "&#39;" not in text


# --- realtime editing ------------------------------------------------------


def test_template_version_changes_when_a_file_is_saved(tmp_path, monkeypatch):
    """This token is what the preview page polls to reload itself."""
    import coldstart.email_template as et

    live = tmp_path / "email"
    live.mkdir()
    (live / "theme.json").write_text(json.dumps({"brand": "X"}))
    (live / HTML_TEMPLATE).write_text("{{ brand }}")
    (live / TEXT_TEMPLATE).write_text("{{ brand }}")
    monkeypatch.setattr(et, "TEMPLATE_DIR", live)

    before = template_version()
    (live / HTML_TEMPLATE).write_text("{{ brand }} edited")
    assert template_version() != before


def test_an_edit_takes_effect_without_a_restart(tmp_path, monkeypatch):
    """The whole point: templates are read from disk on every render."""
    import coldstart.email_template as et

    live = tmp_path / "email"
    live.mkdir()
    (live / "theme.json").write_text(json.dumps({"brand": "Before"}))
    (live / HTML_TEMPLATE).write_text("<p>{{ brand }}</p>")
    monkeypatch.setattr(et, "TEMPLATE_DIR", live)

    first = render_template(HTML_TEMPLATE, sections=_sections(), run_date=date(2026, 8, 20))
    assert "Before" in first

    (live / "theme.json").write_text(json.dumps({"brand": "After"}))
    second = render_template(HTML_TEMPLATE, sections=_sections(), run_date=date(2026, 8, 20))
    assert "After" in second


# --- the fallback layout itself --------------------------------------------


def test_the_fallback_carries_every_job_and_says_it_is_the_fallback():
    """Safety net, so it gets tested directly rather than only through the
    broken-template path. Deliberately plain — a broken template should
    announce itself in the inbox rather than pass unnoticed."""
    from coldstart.digest import _fallback_html

    sections = _sections(
        strong=[_job(global_id="a", score=91)],
        consider=[_job(global_id="b", score=64, apply_url=None)],
    )
    out = _fallback_html(sections, date(2026, 8, 20))

    assert "built-in fallback layout" in out
    assert "Senior Software Engineer" in out
    assert 'href="https://example.com/apply"' in out
    assert "no link" in out  # the linkless job still appears
    assert "Unresolved errors: 0" in out


def test_the_fallback_escapes_html_too():
    from coldstart.digest import _fallback_html

    job = _job(company="<script>alert(1)</script>")
    out = _fallback_html(_sections(strong=[job]), date(2026, 8, 20))
    assert "<script>alert" not in out
    assert "&lt;script&gt;" in out


def test_the_fallback_text_carries_every_job():
    from coldstart.digest import _fallback_text

    sections = _sections(strong=[_job(score=91)], consider=[_job(global_id="b", score=64)])
    text = _fallback_text(sections, date(2026, 8, 20))
    assert "built-in fallback layout" in text
    assert "[91]" in text and "[64]" in text
    assert "https://example.com/apply" in text


def test_the_fallback_handles_an_empty_digest():
    from coldstart.digest import _fallback_html, _fallback_text

    assert "No new matches today" in _fallback_html(_sections(), date(2026, 8, 20))
    assert "No new matches today" in _fallback_text(_sections(), date(2026, 8, 20))
