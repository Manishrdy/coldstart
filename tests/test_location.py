import json
import logging
from pathlib import Path

import pandas as pd
import pytest

from coldstart.filters.location import classify_location, filter_locations

FIXTURES_PATH = Path(__file__).parent / "fixtures" / "locations.json"
CASES = json.loads(FIXTURES_PATH.read_text())


@pytest.mark.parametrize("case", CASES, ids=[c["note"] for c in CASES])
def test_location_fixture_cases(case):
    inp = case["input"]
    flag, reason = classify_location(
        inp.get("location"), inp.get("country_iso"), inp.get("is_remote")
    )
    assert flag.value == case["expected_flag"], (
        f"input={inp!r} note={case['note']!r} got reason={reason!r}"
    )


def test_classify_location_handles_nan_like_pandas_missing_values():
    nan = float("nan")
    flag, reason = classify_location(nan, nan, nan)
    assert flag.value == "uncertain"
    assert reason == "no_location_data"


def test_classify_location_nan_is_remote_does_not_trigger_north_america():
    flag, _ = classify_location("Remote - North America", None, float("nan"))
    assert flag.value == "uncertain"


def test_filter_locations_handles_real_dataframe_with_nan_missing_values():
    # Mirrors what pandas actually produces from parquet: missing values are
    # NaN (float), not None — this is the exact shape that broke classify_location
    # before _clean_str/is_remote normalization was added.
    df = pd.DataFrame(
        {"location": [None, None, "California"], "country_iso": [None] * 3, "is_remote": [None] * 3}
    )
    result = filter_locations(df)
    assert result["location"].isna().sum() == 2  # confirms pandas really did store NaN
    assert list(result["location_flag"]) == ["uncertain", "uncertain", "accepted"]


def test_filter_locations_adds_columns():
    df = pd.DataFrame(
        {
            "global_id": ["a", "b", "c"],
            "location": ["California", "Toronto, Canada", None],
            "country_iso": [None, None, None],
            "is_remote": [False, False, None],
        }
    )
    result = filter_locations(df)
    assert list(result["location_flag"]) == ["accepted", "rejected", "uncertain"]
    assert "location_reason" in result.columns


def test_filter_locations_does_not_mutate_input():
    df = pd.DataFrame({"location": ["California"], "country_iso": [None], "is_remote": [False]})
    original_columns = list(df.columns)
    filter_locations(df)
    assert list(df.columns) == original_columns


def test_filter_locations_never_drops_a_row():
    df = pd.DataFrame(
        {
            "location": ["California", None, "Toronto, Canada", ""],
            "country_iso": [None, None, None, None],
            "is_remote": [False, None, False, None],
        }
    )
    result = filter_locations(df)
    assert len(result) == len(df)
    assert set(result["location_flag"]) <= {"accepted", "rejected", "uncertain"}


def test_filter_locations_warns_on_high_uncertain_ratio(caplog):
    df = pd.DataFrame(
        {
            "location": [None] * 5 + ["California"],
            "country_iso": [None] * 6,
            "is_remote": [None] * 6,
        }
    )
    with caplog.at_level(logging.WARNING, logger="coldstart.filters.location"):
        filter_locations(df)
    assert any("UNCERTAIN" in record.getMessage() for record in caplog.records)


def test_filter_locations_no_warning_under_threshold(caplog):
    df = pd.DataFrame(
        {
            "location": ["California"] * 9 + [None],
            "country_iso": [None] * 10,
            "is_remote": [None] * 10,
        }
    )
    with caplog.at_level(logging.WARNING, logger="coldstart.filters.location"):
        filter_locations(df)
    assert not any("UNCERTAIN" in record.getMessage() for record in caplog.records)


def test_filter_locations_missing_global_id_column_does_not_crash():
    df = pd.DataFrame({"location": ["California"], "country_iso": [None], "is_remote": [False]})
    result = filter_locations(df)
    assert result["location_flag"].iloc[0] == "accepted"
