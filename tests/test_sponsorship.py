import json

import pandas as pd
import pytest

from coldstart.filters.sponsorship import classify_sponsorship, filter_sponsorship
from coldstart.models import SponsorshipStance

WONT = SponsorshipStance.WONT_SPONSOR
WILL = SponsorshipStance.WILL_SPONSOR

# Every sentence below is from a real posting in the 2026-10-20 validation set
# (filters/sponsorship.py docstring), lightly trimmed.


# --- refusals ---------------------------------------------------------------


@pytest.mark.parametrize(
    "sentence,rule",
    [
        ("Please note that we will not sponsor applicants for work visas for this position.",
         "negated"),
        ("This Position is not eligible for Intel immigration sponsorship.", "negated"),
        ("Candidates must possess authorization to work in the United States, as it is not "
         "our practice to sponsor individuals for work visas.", "negated"),
        ("The employer is unable to provide or continue work authorization sponsorship for "
         "this role.", "negated"),
        ("Applicants must have a valid work authorization that does not now, or in the "
         "future, require visa sponsorship for employment in the United States.", "negated"),
        ("We are unable to sponsor or take over sponsorship of an employment visa.", "negated"),
        ("Currently, we are not accepting H-1B applicants or sponsoring new H-1B visas.",
         "negated"),
        ("We can't sponsor visas now or in the future.", "negated"),
        ("Seattle, WA (On-site only); no sponsorship", "no"),
        ("Visa Sponsorship: Will sponsor TN visa or equivalent. No H1B.", "no"),
        ("Applicants must be legally authorized to work in the United States without the need "
         "for current or future employment-based visa sponsorship.", "without"),
        ("Must be legally able to work in the US without requirement of any type of visa "
         "sponsorship or transfer, now, or at any time in the future.", "without"),
        ("Candidates must be authorised to work in the US without current or future "
         "employer-sponsored visa support.", "without"),
        ("Visa sponsorship is not available for this position.", "unavailable"),
        ("Visa sponsorship is not currently available.", "unavailable"),
        ("U.S. work visa sponsorship (such as TN, O-1, H-1B, etc.) is not available for this "
         "role now or in the future.", "unavailable"),
        ("Please note that sponsorship of new applicants for employment authorization, or any "
         "other immigration-related support, is not available for this position.",
         "unavailable"),
        ("This position is not eligible for CPT, OPT, or visa sponsorship, now or in the "
         "future.", "negated"),
        ("Please note that for this position, we are not able to consider candidates who "
         "currently or in the future will require visa sponsorship.", "not_considered"),
    ],
)
def test_real_refusals_are_caught(sentence, rule):
    stance, reason = classify_sponsorship(f"Build backend services in Python. {sentence}", None)
    assert stance is WONT
    assert reason.startswith(f"{rule}:")


def test_markdown_emphasis_does_not_hide_the_negation():
    """Real bug in the first draft: the bold markers split "does not" apart,
    so "provide visa support" read as an offer."""
    stance, _ = classify_sponsorship(
        "Please note our company does **not** provide visa support or sponsorship for employees.",
        None,
    )
    assert stance is WONT


def test_a_refusal_beats_an_offer_of_h1b_transfers():
    """Someone on OPT needs a *new* petition; a company that only transfers
    existing H-1Bs is a refusal for them."""
    stance, _ = classify_sponsorship(
        "We are able to support H-1B transfers but are not initiating new visa sponsorships "
        "at this time.",
        None,
    )
    assert stance is WONT


# --- form fields --------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("Relocation Assistance : Not Available Sponsorship Available : Not Available", WONT),
        ("Is role eligible for Immigration Sponsorship? No. Please note that we will not "
         "sponsor applicants for work visas.", WONT),
        ("Is role eligible for Immigration Sponsorship?: Yes", WILL),
        ("**Visa sponsorship:** Yes, we sponsor visas.", WILL),
        ("**Visa Sponsorship: Not available for this role**", WONT),
    ],
)
def test_form_field_answers_decide(text, expected):
    stance, reason = classify_sponsorship(text, None)
    assert stance is expected
    assert reason.startswith("field:")


def test_a_field_yes_is_not_overridden_by_a_neighbouring_field_no():
    """The prose patterns can run across a field boundary that has no
    punctuation in it — "Not Available Sponsorship" — so the field wins."""
    stance, _ = classify_sponsorship(
        "Relocation Assistance : Not Available Sponsorship Available : Yes", None
    )
    assert stance is WILL


# --- offers -----------------------------------------------------------------


@pytest.mark.parametrize(
    "sentence",
    [
        "Visa sponsorship is available for this position.",
        "We do sponsor and take over sponsorship of employment visas for this role.",
        "We're happy to sponsor international candidates (e.g., STEM OPT, OPT, H1B, O1, E3).",
        "Retell AI is open to sponsoring work authorization for qualified candidates.",
        "GEICO will consider sponsoring a new qualified applicant for employment authorization.",
        "Benefits: - Visa Sponsorship - Hybrid work: 3 days in San Francisco office",
        "Full visa sponsorship and relocation support",
    ],
)
def test_real_offers_are_recognised(sentence):
    stance, _ = classify_sponsorship(sentence, None)
    assert stance is WILL


# --- things that are not refusals -------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        # Hedges from companies that do sponsor.
        "We sponsor visas: H1B, O1, EB1, L1, STEM + OPT, and more. We can't successfully "
        "sponsor for every role and every candidate.",
        "US work authorization is preferred but not required, can sponsor visas.",
        "We sponsor visas. While we can't guarantee success for every candidate, we try.",
    ],
)
def test_hedges_from_sponsoring_companies_are_not_refusals(text):
    stance, _ = classify_sponsorship(text, None)
    assert stance is WILL


@pytest.mark.parametrize(
    "text",
    [
        # "Sponsor" with nothing to do with visas.
        "In addition to our traditional benefits, we sponsor events throughout the year.",
        "Health insurance, paid time off, and other company-sponsored employee benefits are "
        "not provided under the freelance contract.",
        "Duties include, but are not limited to, sponsoring internal hackathons.",
        "Regular sponsored professional development opportunities.",
        # Work authorisation without a word about sponsorship.
        "Candidates must be authorized to work in the United States.",
        "Applicants can work in the US with or without sponsorship.",
        "Software Engineer at a growing startup.",
    ],
)
def test_unrelated_or_silent_text_has_no_stance(text):
    stance, reason = classify_sponsorship(text, None)
    assert stance is None
    assert reason is None


def test_empty_text_has_no_stance():
    assert classify_sponsorship(None, None) == (None, None)
    assert classify_sponsorship(float("nan"), float("nan")) == (None, None)


# --- ycombinator's own tag --------------------------------------------------


@pytest.mark.parametrize(
    "tag,expected",
    [
        ("Will sponsor", WILL),
        ("US citizen/visa only", SponsorshipStance.EXISTING_AUTHORIZATION_ONLY),
        ("US citizenship/visa not required", None),
    ],
)
def test_yc_visa_tag(tag, expected):
    raw = json.dumps({"visa": tag, "role": "eng"})
    stance, reason = classify_sponsorship("Founding engineer, Python and TypeScript.", raw)
    assert stance is expected
    assert reason == (f"yc_visa:{tag}" if expected else None)


def test_yc_tag_is_read_from_a_dict_too():
    stance, _ = classify_sponsorship("", {"visa": "Will sponsor"})
    assert stance is WILL


def test_the_description_wins_over_the_yc_tag():
    raw = json.dumps({"visa": "Will sponsor"})
    stance, reason = classify_sponsorship("We are unable to sponsor visas for this role.", raw)
    assert stance is WONT
    assert reason.startswith("negated:")


def test_unparseable_raw_is_ignored():
    stance, _ = classify_sponsorship("Python role.", '{"visa": "Will sponsor"')
    assert stance is None


# --- filter_sponsorship -----------------------------------------------------


def _frame() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "global_id": ["a", "b", "c"],
            "description": [
                "We will not sponsor visas for this role.",
                "Visa sponsorship is available.",
                "Build things in Python.",
            ],
            "raw": [None, None, None],
        }
    )


def test_filter_marks_refusals_excluded_only_when_enabled():
    on = filter_sponsorship(_frame(), enabled=True)
    assert list(on["sponsorship_excluded"]) == [True, False, False]
    assert list(on["sponsorship"]) == ["wont_sponsor", "will_sponsor", None]
    assert on["sponsorship_reason"].iloc[0].startswith("negated:")

    off = filter_sponsorship(_frame(), enabled=False)
    assert list(off["sponsorship_excluded"]) == [False, False, False]
    # The stance is still recorded — it is a ranking signal either way.
    assert list(off["sponsorship"]) == ["wont_sponsor", "will_sponsor", None]


def test_filter_preserves_rows_and_does_not_mutate_input():
    df = _frame()
    out = filter_sponsorship(df, enabled=True)
    assert len(out) == len(df)
    assert "sponsorship" not in df.columns


def test_filter_handles_an_empty_frame_and_missing_columns():
    empty = filter_sponsorship(pd.DataFrame({"global_id": []}), enabled=True)
    assert len(empty) == 0
    assert {"sponsorship", "sponsorship_reason", "sponsorship_excluded"} <= set(empty.columns)

    no_text = filter_sponsorship(pd.DataFrame({"global_id": ["x"]}), enabled=True)
    assert list(no_text["sponsorship_excluded"]) == [False]
