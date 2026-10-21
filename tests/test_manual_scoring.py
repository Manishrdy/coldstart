from __future__ import annotations

import pytest

from coldstart.models import RawJob, ScoreBand
from coldstart.scoring.manual import score_manually, skills_in
from coldstart.settings import Settings

_RESUME = "SKILLS\nPython, TypeScript, AWS, PostgreSQL, LLMs, RAG, Docker\n"


def _settings(**overrides) -> Settings:
    kwargs = dict(
        experience_years=3.5,
        smtp_user="user@example.com",
        smtp_app_password="app-pw",
        digest_recipient="user@example.com",
        score_threshold_strong=80,
    )
    kwargs.update(overrides)
    return Settings(**kwargs)


def _job(title: str = "Software Engineer, Growth", description: str = "") -> RawJob:
    return RawJob(
        global_id="ashby:1",
        company="base-power",
        title=title,
        url="https://x/1",
        ats_type="ashby",
        description=description,
    )


def test_finds_the_skills_a_posting_names():
    text = "We use Python, TypeScript and React on AWS, with PostgreSQL and Kafka."
    assert skills_in(text) == ["Python", "TypeScript", "React", "PostgreSQL", "Kafka", "AWS"]


@pytest.mark.parametrize(
    "text",
    [
        "Help us go to market faster.",
        "You'll work with the rest of the team.",
        "React quickly to incidents.",
        "Spark joy in our customers.",
    ],
)
def test_ordinary_words_are_not_skills(text):
    assert skills_in(text) == []


def test_go_counts_when_listed_as_a_language():
    assert "Go" in skills_in("Backend services in Python, Go, and Rust.")
    assert "Go" in skills_in("Experience with Golang.")


def test_a_close_match_scores_strong_and_says_why():
    job = _job(description="Python, TypeScript, AWS and PostgreSQL. Build RAG over LLMs.")

    score = score_manually(job, _RESUME, _settings(), reason="base-power hit its limit")

    assert score.eligible is True
    assert score.tech_stack_match == 100
    assert score.matched_skills == ["Python", "TypeScript", "PostgreSQL", "AWS", "LLMs", "RAG"]
    assert score.missing_skills == []
    assert score.score >= 80
    assert score.score_band is ScoreBand.STRONG
    assert score.reasoning.startswith(
        "Scored by keyword match, not the LLM: base-power hit its limit."
    )


def test_missing_skills_and_years_pull_the_score_down():
    close = score_manually(_job(description="Python and AWS."), _RESUME, _settings(), reason="x")
    far = score_manually(
        _job(description="Java, Kotlin and Scala on AWS. 9+ years of experience required."),
        _RESUME,
        _settings(),
        reason="x",
    )

    assert far.missing_skills == ["Java", "Kotlin", "Scala"]
    assert far.experience_fit == 15
    assert far.score < close.score
    assert far.score_band is ScoreBand.REJECT


def test_a_senior_title_scores_below_the_same_job_at_mid_level():
    description = "Python and AWS."
    mid = score_manually(_job("Software Engineer", description), _RESUME, _settings(), reason="x")
    senior = score_manually(
        _job("Senior Software Engineer", description), _RESUME, _settings(), reason="x"
    )
    assert senior.score < mid.score


def test_fde_and_ai_titles_score_higher_on_role_type():
    description = "Python and AWS."
    plain = score_manually(_job("Software Engineer", description), _RESUME, _settings(), reason="x")
    fde = score_manually(
        _job("Forward Deployed Engineer", description), _RESUME, _settings(), reason="x"
    )
    assert fde.role_type_fit > plain.role_type_fit


def test_a_posting_with_no_description_still_gets_a_score():
    score = score_manually(_job(description=""), _RESUME, _settings(), reason="x")
    assert 0 <= score.score <= 100
    assert score.matched_skills == [] and score.missing_skills == []
