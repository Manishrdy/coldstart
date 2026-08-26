import json
import logging
from pathlib import Path

import pandas as pd
import pytest

from coldstart.filters.eligibility import classify_eligibility, filter_eligibility

FIXTURES_PATH = Path(__file__).parent / "fixtures" / "eligibility.json"
CASES = json.loads(FIXTURES_PATH.read_text())


@pytest.mark.parametrize("case", CASES, ids=[c["note"] for c in CASES])
def test_eligibility_fixture_cases(case):
    inp = case["input"]
    flag, reason = classify_eligibility(inp.get("description"), inp.get("raw"))
    assert flag.value == case["expected_flag"], (
        f"input={inp!r} note={case['note']!r} got reason={reason!r}"
    )


def test_not_excluded_phrases_are_100_percent_passed():
    not_excluded_cases = [c for c in CASES if "NOT excluded" in c["note"]]
    assert len(not_excluded_cases) >= 4
    for case in not_excluded_cases:
        flag, reason = classify_eligibility(case["input"].get("description"), None)
        assert flag.value == "passed", f"false exclusion on {case['input']!r}: reason={reason!r}"


def test_ear_lowercase_does_not_false_positive():
    flag, _ = classify_eligibility("Please wear appropriate ear protection on site.", None)
    assert flag.value == "passed"


def test_filter_eligibility_adds_columns_and_preserves_rows():
    df = pd.DataFrame(
        {
            "global_id": ["a", "b", "c"],
            "description": [
                "Must be a US citizen.",
                "Great benefits and a friendly team.",
                "Some roles may require clearance.",
            ],
            "raw": [None, None, None],
        }
    )
    result = filter_eligibility(df)
    assert len(result) == 3
    assert list(result["eligibility_flag"]) == ["excluded", "passed", "uncertain"]
    assert "eligibility_reason" in result.columns


def test_filter_eligibility_does_not_drop_excluded_rows():
    df = pd.DataFrame(
        {
            "global_id": ["a"],
            "description": ["Requires an active security clearance."],
            "raw": [None],
        }
    )
    result = filter_eligibility(df)
    assert len(result) == 1
    assert result["eligibility_flag"].iloc[0] == "excluded"


def test_filter_eligibility_warns_on_excluded(caplog):
    df = pd.DataFrame(
        {"global_id": ["job-123"], "description": ["Must be a US citizen."], "raw": [None]}
    )
    with caplog.at_level(logging.WARNING, logger="coldstart.filters.eligibility"):
        filter_eligibility(df)
    assert any("job-123" in record.getMessage() for record in caplog.records)


def test_filter_eligibility_missing_global_id_column_does_not_crash():
    df = pd.DataFrame({"description": ["Great team."], "raw": [None]})
    result = filter_eligibility(df)
    assert result["eligibility_flag"].iloc[0] == "passed"


def test_filter_eligibility_handles_nan_description_and_raw():
    df = pd.DataFrame({"global_id": ["a"], "description": [float("nan")], "raw": [float("nan")]})
    result = filter_eligibility(df)
    assert result["eligibility_flag"].iloc[0] == "uncertain"


# --- sponsorship stance in `raw` (ycombinator) ------------------------------
# All three are the real values from the live ycombinator slice, where every
# one of 3,419 rows carries a `visa` key. See filters/eligibility.py.


@pytest.mark.parametrize(
    "visa",
    ["Will sponsor", "US citizen/visa only", "US citizenship/visa not required"],
)
def test_visa_stance_in_raw_never_excludes(visa):
    raw = json.dumps({"role": "eng", "visa": visa, "skills": ["Go"]})
    flag, reason = classify_eligibility("Backend Engineer at a startup.", raw)
    assert flag.value == "passed", f"visa={visa!r} wrongly {flag.value} on {reason!r}"


def test_visa_stance_as_dict_is_also_dropped():
    raw = {"role": "eng", "visa": "US citizen/visa only"}
    flag, _ = classify_eligibility("Backend Engineer.", raw)
    assert flag.value == "passed"


def test_dropping_visa_does_not_hide_a_real_bar_in_the_description():
    """The whole point of the carve-out is that it is narrow."""
    raw = json.dumps({"visa": "Will sponsor"})
    flag, reason = classify_eligibility("Requires an active TS/SCI clearance.", raw)
    assert flag.value == "excluded"
    assert "TS/SCI" in reason


def test_other_raw_keys_are_still_scanned():
    raw = json.dumps({"visa": "Will sponsor", "notes": "Must be a US citizen."})
    flag, _ = classify_eligibility("Backend Engineer.", raw)
    assert flag.value == "excluded"


def test_raw_that_is_not_json_still_has_the_stance_stripped():
    raw = 'trailing junk "visa": "US citizen/visa only" not json at all'
    flag, _ = classify_eligibility("Backend Engineer.", raw)
    assert flag.value == "passed"


def test_visa_only_raw_with_empty_description_is_uncertain_not_passed():
    """Dropping the one populated field must not fabricate a pass: with
    nothing left to read the honest answer is still 'no data'."""
    flag, reason = classify_eligibility(None, json.dumps({"visa": "Will sponsor"}))
    assert flag.value == "uncertain"
    assert reason == "no_description_data"
