import pandas as pd
import pytest

from coldstart.filters.stack import classify_stack, filter_stack

X_YEARS = 3.5  # matches settings.experience_years in the real deployment


# --- stack mismatch ---------------------------------------------------------


@pytest.mark.parametrize(
    "description,category",
    [
        ("Senior Software Engineer. Must have 5+ years of Java and Spring Boot.", "java"),
        ("Backend Engineer building services in .NET / C# on Azure.", "dotnet"),
        ("PHP developer needed for our Laravel e-commerce platform.", "other_language"),
        ("DevOps Engineer: Terraform, Ansible, and Kubernetes administration.", "devops_only"),
    ],
)
def test_stack_mismatch_excludes_when_no_candidate_stack_mentioned(description, category):
    excluded, reason = classify_stack(description, None, X_YEARS)
    assert excluded is True
    assert reason.startswith(f"stack_mismatch:{category}:")


def test_candidate_stack_mention_overrides_a_mismatch_keyword():
    """A JD naming Java alongside Python must not be excluded — the whole
    point of the rule is 'never seen the candidate's stack', not 'ever
    mentions a stack the candidate lacks'."""
    description = "Full-stack role: Python backend, some legacy Java services to maintain."
    excluded, reason = classify_stack(description, None, X_YEARS)
    assert excluded is False
    assert reason is None


@pytest.mark.parametrize(
    "keyword",
    ["node.js", "nodejs", "TypeScript", "FastAPI", "Flask", "LangChain", "RAG", "LLM"],
)
def test_each_candidate_stack_keyword_prevents_exclusion(keyword):
    description = f"DevOps Engineer, Terraform and Kubernetes administration. Also uses {keyword}."
    excluded, _ = classify_stack(description, None, X_YEARS)
    assert excluded is False


def test_generic_posting_with_no_stack_signal_at_all_is_never_excluded():
    """No mismatch keyword fires, so this passes regardless of candidate-stack
    presence — the filter only excludes on a positive mismatch signal."""
    excluded, reason = classify_stack("Software Engineer at a growing startup.", None, X_YEARS)
    assert excluded is False
    assert reason is None


def test_empty_text_is_never_excluded():
    excluded, reason = classify_stack(None, None, X_YEARS)
    assert excluded is False
    assert reason is None


def test_javascript_does_not_false_positive_the_java_pattern():
    """'java' with a negative lookahead for 'script' must not fire on
    JavaScript, which is common in Node/React postings the candidate fits."""
    excluded, _ = classify_stack("Frontend Engineer using JavaScript and React.", None, X_YEARS)
    assert excluded is False


# --- experience floor --------------------------------------------------------


def test_explicit_high_years_requirement_excludes():
    excluded, reason = classify_stack(
        "Staff Software Engineer. Requires 10+ years of experience in Python.", None, X_YEARS
    )
    assert excluded is True
    assert reason == "experience_floor:10yrs_required"


def test_years_at_the_rubric_floor_boundary_is_not_excluded():
    """R > X+4 is the rubric's own cutoff (scoring/rubric.py). At X=3.5,
    X+4=7.5, so a stated 7-year requirement does NOT mathematically guarantee
    a sub-threshold score and must not be hard-excluded pre-LLM."""
    excluded, reason = classify_stack(
        "Software Engineer, Python. Minimum 7 years of experience required.", None, X_YEARS
    )
    assert excluded is False
    assert reason is None


def test_years_just_past_the_rubric_floor_boundary_is_excluded():
    excluded, reason = classify_stack(
        "Software Engineer, Python. Minimum 8 years of experience required.", None, X_YEARS
    )
    assert excluded is True
    assert reason == "experience_floor:8yrs_required"


def test_experience_floor_fires_even_when_candidate_stack_is_present():
    """Orthogonal to the stack check — a Python/AI role with an excessive
    years floor is still a guaranteed sub-threshold score."""
    excluded, reason = classify_stack(
        "AI Engineer using Python, LangChain, and RAG. 12+ years required.", None, X_YEARS
    )
    assert excluded is True
    assert reason == "experience_floor:12yrs_required"


def test_years_mentioned_without_a_qualifying_context_does_not_exclude():
    """A bare number near the word 'years' with no requirement framing (e.g.
    company age) must not be mistaken for a minimum-experience bar."""
    excluded, reason = classify_stack(
        "Our company has been building great products for 15 years. Python role.",
        None,
        X_YEARS,
    )
    assert excluded is False
    assert reason is None


# --- filter_stack (DataFrame) ------------------------------------------------


def test_filter_stack_adds_columns_and_preserves_rows():
    df = pd.DataFrame(
        {
            "global_id": ["a", "b", "c"],
            "description": [
                "Java Spring Boot backend engineer, enterprise systems.",
                "Python/FastAPI backend engineer building AI agents.",
                "DevOps Engineer: Terraform, Ansible.",
            ],
            "raw": [None, None, None],
        }
    )
    result = filter_stack(df, X_YEARS)
    assert len(result) == 3
    assert list(result["stack_excluded"]) == [True, False, True]
    assert "stack_reason" in result.columns
    assert pd.isna(result["stack_reason"].iloc[1])


def test_filter_stack_missing_global_id_column_does_not_crash():
    df = pd.DataFrame({"description": ["Great team, Python role."], "raw": [None]})
    result = filter_stack(df, X_YEARS)
    assert not result["stack_excluded"].iloc[0]


# --- regressions: false positives caught by sampling real matched JD text ---
# (2026-09-02) rather than trusting aggregate counts. See the module
# docstring in filters/stack.py for the full story.


def test_url_domain_does_not_false_positive_the_dotnet_pattern():
    """A press-release boilerplate link like 'c212.net/c/link' must not read
    as a .NET requirement — the naive case-insensitive '\\.net\\b' pattern
    matched this on a real Boston Scientific posting."""
    description = "Learn more at https://c212.net/c/link/?t=0&l=en&o=4007299-1 about our mission."
    excluded, reason = classify_stack(description, None, X_YEARS)
    assert excluded is False
    assert reason is None


def test_dotnet_still_fires_on_a_real_requirement():
    excluded, reason = classify_stack(
        "Backend Engineer. Strong experience with the .NET Framework required.", None, X_YEARS
    )
    assert excluded is True
    assert reason == "stack_mismatch:dotnet:.NET"


def test_salesforce_as_a_founders_past_employer_is_not_a_stack_requirement():
    """Real false positive: a healthcare AI startup's bio said the founder
    'led AI/ML teams at Salesforce' — Salesforce the past employer, not a
    job requirement. Bare 'salesforce' was dropped from the lexicon for
    exactly this reason (~37% false-positive rate sampled from real data);
    only specific compound phrases remain."""
    description = (
        "Backend Engineer building AI agents for clinics. Our founder previously "
        "led AI/ML teams at Salesforce before starting this company."
    )
    excluded, reason = classify_stack(description, None, X_YEARS)
    assert excluded is False
    assert reason is None


def test_salesforce_developer_role_still_excludes():
    excluded, reason = classify_stack(
        "We need a Salesforce Developer to build custom Apex triggers.", None, X_YEARS
    )
    assert excluded is True
    assert reason.startswith("stack_mismatch:other_language:")


def test_years_of_age_is_never_read_as_an_experience_requirement():
    """Real false positive: 'At least 21 years of age' is legal-age
    boilerplate present in nearly every US posting, not an experience bar."""
    excluded, reason = classify_stack(
        "Software Engineer, Python. Must be at least 21 years of age. "
        "3+ years of experience required.",
        None,
        X_YEARS,
    )
    # The genuine 3-year requirement is well under the floor, so this must
    # pass — a bug here would wrongly read 21 as the requirement instead.
    assert excluded is False
    assert reason is None


def test_company_history_years_are_never_read_as_an_experience_requirement():
    """Real false positive: 'a 90+ year history' / '40+ years at Palantir'
    (company tenure, founder bio) were misread as candidate requirements on
    several real postings, including a Forward Deployed Engineer role."""
    description = (
        "Forward Deployed Engineer. The founding team spent a combined 40+ years "
        "at Palantir building the type of software this role extends. "
        "5+ years building production software required."
    )
    excluded, reason = classify_stack(description, None, X_YEARS)
    assert excluded is False
    assert reason is None


def test_range_uses_the_low_end_not_the_high_end():
    """Real false positive: 'Senior Applied AI/ML Engineer, 5-8+ years of
    experience' was misread as an 8-year floor. The actual floor is the
    range's low end (5), which this candidate (3.5 years) doesn't clear
    outright but which does NOT mathematically guarantee a sub-threshold
    score the way 8 does — so it must not be excluded."""
    excluded, reason = classify_stack(
        "Senior Applied AI/ML Engineer. 5-8+ years of experience building "
        "software systems using Python and LLMs.",
        None,
        X_YEARS,
    )
    assert excluded is False
    assert reason is None


def test_range_high_end_still_excludes_when_genuinely_past_the_floor():
    excluded, reason = classify_stack(
        "Software Engineer, Python. 8-12 years of experience required.", None, X_YEARS
    )
    assert excluded is True
    assert reason == "experience_floor:8yrs_required"
