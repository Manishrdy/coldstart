from __future__ import annotations

import pandas as pd
import pytest

from coldstart.filters.company import filter_companies, is_excluded_company

# Every company Manish named, plus the parent-company aliases.
BLOCKED = [
    "Amazon",
    "Tesla",
    "Apple",
    "TikTok",
    "ByteDance",
    "Google",
    "Alphabet",
    "Uber",
    "Meta",
    "Facebook",
]


@pytest.mark.parametrize("name", BLOCKED)
def test_every_named_company_is_blocked(name):
    excluded, _matched = is_excluded_company(name)
    assert excluded


@pytest.mark.parametrize("name", BLOCKED)
def test_blocking_is_case_and_whitespace_insensitive(name):
    assert is_excluded_company(f"  {name.upper()}  ")[0]
    assert is_excluded_company(name.lower())[0]


@pytest.mark.parametrize(
    "name",
    [
        "Amazon.com, Inc.",
        "Meta Platforms, Inc.",
        "Google LLC",
        "Uber Technologies",
        "Apple Inc.",
        "meta_platforms",
        "Tesla, Inc",
    ],
)
def test_legal_suffixes_and_separators_do_not_evade_the_block(name):
    assert is_excluded_company(name)[0], name


def test_a_careers_hostname_is_matched_on_its_first_label():
    """The real leak this filter exists for: excluded_ats.json blocks Amazon's
    own feed, but Amazon postings also reach us via the personio slice."""
    excluded, matched = is_excluded_company("amazon.jobs.personio.com")
    assert (excluded, matched) == (True, "amazon")


# --- the false positives that matter --------------------------------------
# Every one of these is a real company observed in the cached parquet data.
# Substring matching would silently destroy real opportunities here, which is
# exactly the failure mode this project exists to avoid.


@pytest.mark.parametrize(
    "name",
    [
        "apple-roofing",          # 58 real postings on the ashby slice
        "Apple Roofing",
        "Meta House",             # paycom
        "Meta Group,",            # breezy
        "meta-agility",           # join_com
        "meta-five",
        "meta-bot",
        "Metabase",
        "Googol Analytics",
        "Uberflip",
        "Applebee's",
        "Teslar Software",
        "Amazonas Logistica",
    ],
)
def test_unrelated_companies_are_never_blocked(name):
    excluded, matched = is_excluded_company(name)
    assert not excluded, f"{name!r} was wrongly blocked (matched {matched!r})"


# --- messy real data -------------------------------------------------------


@pytest.mark.parametrize("value", [None, "", "   ", 12345, float("nan")])
def test_missing_or_non_string_company_never_raises(value):
    assert is_excluded_company(value) == (False, "")


def test_filter_drops_blocked_rows_and_keeps_the_rest():
    df = pd.DataFrame(
        {
            "company": ["Amazon", "apple-roofing", "Google LLC", "Stripe", "Meta House"],
            "title": ["Software Engineer"] * 5,
        }
    )
    out = filter_companies(df)
    assert list(out["company"]) == ["apple-roofing", "Stripe", "Meta House"]
    assert list(out.index) == [0, 1, 2]  # reindexed for the downstream filters


def test_filter_logs_a_warning_naming_each_blocked_company(caplog):
    df = pd.DataFrame({"company": ["Amazon", "Stripe"], "title": ["Software Engineer"] * 2})
    with caplog.at_level("WARNING"):
        filter_companies(df)
    assert "Amazon" in caplog.text
    assert "Stripe" not in caplog.text


def test_filter_is_a_no_op_when_nothing_is_blocked():
    df = pd.DataFrame({"company": ["Stripe", "Ramp"], "title": ["Software Engineer"] * 2})
    assert list(filter_companies(df)["company"]) == ["Stripe", "Ramp"]


def test_filter_tolerates_a_missing_company_column():
    df = pd.DataFrame({"title": ["Software Engineer"]})
    assert len(filter_companies(df)) == 1


# --- subsidiaries ----------------------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["uberfreight", "Uber Freight", "uber-freight", "googlefiber", "Google Fiber", "GOOGLEFIBER"],
)
def test_known_subsidiaries_are_blocked_in_both_spellings(name):
    """Both post on greenhouse under a run-together slug; 8 of their postings
    survive the title filter and would otherwise reach the LLM."""
    assert is_excluded_company(name)[0], name


@pytest.mark.parametrize(
    "name",
    [
        "uberall",            # real German SaaS company, ashby
        "uberether",          # jazzhr
        "nkuber",             # join_com
        "quberesearchandtechnologies",
        "googol Analytics",
        "schubergphilis",
        "facebook761",        # a French recruiter advertising via Facebook, not Meta
        "google731",          # a Colombian recruiter, not Google
        "metaview",
        "metabolon",
    ],
)
def test_run_together_matching_does_not_over_reach(name):
    excluded, matched = is_excluded_company(name)
    assert not excluded, f"{name!r} was wrongly blocked (matched {matched!r})"
