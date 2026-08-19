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
