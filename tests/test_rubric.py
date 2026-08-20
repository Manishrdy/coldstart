import pytest

from coldstart.models import RawJob
from coldstart.scoring.rubric import (
    RUBRIC_VERSION,
    WEIGHTS,
    build_system_prompt,
    build_user_prompt,
    composite_score,
    experience_fit_score,
)

X = 1.5

# filter_resume_for_llm (Module 19 privacy pass) requires a recognized
# section header or it raises — plain placeholder strings no longer work.
_RESUME = "WORK EXPERIENCE\nAcme\n• Built things.\n"


def _job(**overrides) -> RawJob:
    kwargs = dict(
        global_id="g:1",
        company="Acme",
        title="Software Engineer",
        url="https://x",
        ats_type="greenhouse",
        location="Remote — US",
        description="Great JD body.",
    )
    kwargs.update(overrides)
    return RawJob(**kwargs)


# --- experience_fit_score ------------------------------------------------------


def test_experience_fit_score_r_none_returns_none():
    assert experience_fit_score(None, X) is None


@pytest.mark.parametrize(
    "r,expected",
    [
        (1, 100),
        (X, 100),  # R == X exactly
        (X + 2, 80),  # R == X+2 exactly
        (X + 4, 40),  # R == X+4 exactly
        (6, 15),
        (10, 15),
    ],
)
def test_experience_fit_score_exact_boundaries(r, expected):
    assert experience_fit_score(r, X) == expected


def test_experience_fit_score_interior_of_first_segment_is_between_80_and_100():
    score = experience_fit_score(3, X)  # between X and X+2
    assert 80 <= score <= 100


def test_experience_fit_score_interior_of_second_segment_is_between_40_and_80():
    score = experience_fit_score(4.5, X)  # between X+2 and X+4
    assert 40 <= score <= 80


def test_experience_fit_score_monotonic_non_increasing():
    r_values = [i / 4 for i in range(0, 80)]  # 0.0 .. 19.75 in 0.25 steps
    scores = [experience_fit_score(r, X) for r in r_values]
    for earlier, later in zip(scores, scores[1:], strict=False):
        assert later <= earlier


def test_experience_fit_score_r_zero_is_full_score():
    assert experience_fit_score(0, X) == 100


def test_experience_fit_score_different_x_shifts_the_curve():
    # same R, larger X (more experience) should never score worse
    assert experience_fit_score(5, x_years=1.0) <= experience_fit_score(5, x_years=4.0)


# --- composite_score -------------------------------------------------------------


def test_weights_sum_to_one():
    assert sum(WEIGHTS.values()) == pytest.approx(1.0)


def test_composite_score_math():
    sub = {"tech_stack": 80, "experience": 100, "seniority": 60, "role_type": 50}
    expected = 80 * 0.35 + 100 * 0.25 + 60 * 0.25 + 50 * 0.15
    assert composite_score(sub) == round(expected)


def test_composite_score_all_100_is_100():
    sub = dict.fromkeys(WEIGHTS, 100)
    assert composite_score(sub) == 100


def test_composite_score_all_zero_is_zero():
    sub = dict.fromkeys(WEIGHTS, 0)
    assert composite_score(sub) == 0


# --- build_system_prompt ----------------------------------------------------------


def test_system_prompt_contains_resume_text():
    prompt = build_system_prompt("WORK EXPERIENCE\nUNIQUE RESUME MARKER 12345", x_years=X)
    assert "UNIQUE RESUME MARKER 12345" in prompt


def test_system_prompt_contains_eligibility_safety_net():
    prompt = build_system_prompt(_RESUME, x_years=X)
    assert "citizenship" in prompt.lower()
    assert "clearance" in prompt.lower()
    assert "ITAR" in prompt
    assert '"eligible": false' in prompt or "eligible\": false" in prompt


def test_system_prompt_contains_seniority_soft_penalty_note():
    prompt = build_system_prompt(_RESUME, x_years=X)
    assert "never a disqualifier" in prompt.lower() or "soft" in prompt.lower()


def test_system_prompt_contains_rubric_version():
    prompt = build_system_prompt(_RESUME, x_years=X)
    assert RUBRIC_VERSION in prompt


def test_system_prompt_contains_yoe_table_with_interpolated_x():
    prompt = build_system_prompt(_RESUME, x_years=X)
    assert f"{X:.2f}" in prompt
    assert f"{X + 2:.2f}" in prompt
    assert f"{X + 4:.2f}" in prompt


def test_system_prompt_element_ordering():
    # "MY RESUME MARKER" alone would itself look like an all-caps section
    # header to filter_resume_for_llm — a bullet line avoids that.
    prompt = build_system_prompt("WORK EXPERIENCE\n• MY RESUME MARKER", x_years=X)
    role_pos = prompt.lower().index("expert technical recruiter")
    weights_pos = prompt.lower().index("tech stack")
    yoe_pos = prompt.lower().index("years-of-experience banding")
    seniority_pos = prompt.lower().index("soft scoring penalty")
    eligibility_pos = prompt.lower().index("eligibility safety net")
    resume_pos = prompt.index("MY RESUME MARKER")
    schema_pos = prompt.lower().index("respond with only strict json")

    assert (
        role_pos
        < weights_pos
        < yoe_pos
        < seniority_pos
        < eligibility_pos
        < resume_pos
        < schema_pos
    )


def test_system_prompt_mentions_every_jobscore_score_field():
    from coldstart.models import JobScore

    prompt = build_system_prompt(_RESUME, x_years=X)
    for field in ("tech_stack_match", "seniority_fit", "experience_fit", "role_type_fit"):
        assert field in JobScore.model_fields
        assert field in prompt


def test_system_prompt_deterministic_for_fixed_inputs():
    a = build_system_prompt(_RESUME, x_years=2.3)
    b = build_system_prompt(_RESUME, x_years=2.3)
    assert a == b


def test_system_prompt_accepts_custom_rubric_version():
    prompt = build_system_prompt(_RESUME, x_years=X, rubric_version="v2-experimental")
    assert "v2-experimental" in prompt
    assert RUBRIC_VERSION not in prompt or RUBRIC_VERSION == "v2-experimental"


# --- build_user_prompt -------------------------------------------------------------


def test_user_prompt_contains_title_company_location():
    job = _job(title="AI Engineer", company="Acme Corp", location="NYC")
    prompt = build_user_prompt(job)
    assert "AI Engineer" in prompt
    assert "Acme Corp" in prompt
    assert "NYC" in prompt


def test_user_prompt_truncates_long_description():
    job = _job(description="x" * 20000)
    prompt = build_user_prompt(job)
    assert prompt.count("x") <= 8000


def test_user_prompt_handles_missing_location():
    job = _job(location=None)
    prompt = build_user_prompt(job)
    assert "Not specified" in prompt


def test_user_prompt_handles_missing_description():
    job = _job(description=None)
    prompt = build_user_prompt(job)
    assert "Job description:" in prompt


def test_user_prompt_deterministic():
    job = _job()
    assert build_user_prompt(job) == build_user_prompt(job)
