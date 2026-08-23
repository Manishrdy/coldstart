import json
import logging
from pathlib import Path

import pandas as pd
import pytest

from coldstart.filters.location import (
    _AMBIGUOUS_ISO2,
    _US_ONLY_ABBRS,
    _external_path_slug,
    _has_non_ascii_letter,
    _parts,
    _postal_anchored_country,
    _to_bool,
    _trailing_foreign_iso2,
    classify_location,
    filter_locations,
)

FIXTURES_PATH = Path(__file__).parent / "fixtures" / "locations.json"
CASES = json.loads(FIXTURES_PATH.read_text())


@pytest.mark.parametrize("case", CASES, ids=[c["note"] for c in CASES])
def test_location_fixture_cases(case):
    inp = case["input"]
    flag, reason = classify_location(
        inp.get("location"), inp.get("country_iso"), inp.get("is_remote"), inp.get("raw")
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


# --- individual signal helpers ---------------------------------------------
# Each aggressive rule gets its own direct test: a failure in the fixture table
# above tells you a location classified wrong, but not which of the six rules
# in the cascade was responsible.


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("false", False), ("true", True), ("False", False), ("0", False),
        ("no", False), ("", False), ("none", False), ("1", True), ("yes", True),
        (True, True), (False, False), (None, False), (float("nan"), False),
    ],
)
def test_to_bool_does_not_trust_pythons_truthy_strings(value, expected):
    # phenom.parquet stores is_remote as the strings 'false'/'true' (1,050
    # rows) and bool("false") is True.
    assert _to_bool(value) is expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('{"externalPath": "/job/Bengaluru/Eng_JR1"}', "Bengaluru"),
        ('{"externalPath": "/job/Xiamen-Fujian-China/SWE_JR2"}', "Xiamen Fujian China"),
        ('{"bullet_fields": ["JR1"], "externalPath": "/job/Pune/Eng"}', "Pune"),
        ("not json", ""),
        ("{}", ""),
        ('{"externalPath": 42}', ""),
        ('{"externalPath": "/job"}', ""),
        ("[1, 2]", ""),
        (None, ""),
        (float("nan"), ""),
        ({"externalPath": "/job/Pune/Eng"}, ""),  # a dict, not the JSON string workday sends
    ],
)
def test_external_path_slug_survives_every_malformed_shape(raw, expected):
    assert _external_path_slug(raw) == expected


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        ("München, BY, DE, 80809", "DE"),
        ("Bremen, DE, 28197", "DE"),
        ("Den Haag, NL, 2597 AK", "NL"),
        ("Heredia, CR, 40101", "CR"),
        ("Abingdon, LND, GB, OX14 4RW", "GB"),
        ("Montreal, Quebec, CA, H3B 4T9", "CA"),
        # US "City, ST, ZIP" has no US token — the state must not be read as a country.
        ("Fate, TX, 75189", None),
        ("Enfield, CT, 06082", None),
        ("Irvine, CA, 92617", None),
        ("Madison, IN, 47250", None),
        ("San Juan, PR, 00901", None),
        ("New York, NY, US, 10036", None),
        ("Remote", None),
    ],
)
def test_postal_anchored_country(location, expected):
    assert _postal_anchored_country(_parts(location)) == expected


@pytest.mark.parametrize(
    ("location", "expected"),
    [
        ("Herzliya, HA, IL", "IL"),
        ("Indore, MP, IN", "IN"),
        ("Mississauga, ON, ca", "CA"),
        ("Hannover, NDS, de", "DE"),
        ("Galway, UNAVAILABLE, IE", "IE"),
        # Fewer than three parts: these US cities are absent from us_cities.json.
        ("San Carlos, CA", None),
        ("Somerville, MA", None),
        # A street address supplies the third comma and GA is Gabon's ISO2.
        ("777 Hemlock St, Macon, GA", None),
        ("New York, NY, US", None),
    ],
)
def test_trailing_foreign_iso2(location, expected):
    assert _trailing_foreign_iso2(_parts(location)) == expected


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("München", True), ("София", True), ("新竹", True), ("Café", True),
        # Punctuation above U+007F is not a letter — an en-dash must not count.
        ("Remote – California Bay Area", False),
        ("Location:Carlsbad – California, USA", False),
        ("Remote — US", False),
        ("Austin, TX", False),
    ],
)
def test_has_non_ascii_letter_tests_letters_not_codepoints(text, expected):
    assert _has_non_ascii_letter(text) is expected


def test_ambiguous_iso2_is_derived_and_complete():
    # Hand-typing this set is how the "Macon, GA -> Gabon" regression returns.
    assert {"GA", "IN", "CA", "DE", "IL", "VA", "MD"} <= _AMBIGUOUS_ISO2
    assert "PR" not in _AMBIGUOUS_ISO2  # Puerto Rico is a US territory
    assert "US" not in _AMBIGUOUS_ISO2
    assert _AMBIGUOUS_ISO2.isdisjoint(_US_ONLY_ABBRS)
    assert {"TX", "NY", "OH", "WA"} <= _US_ONLY_ABBRS


def test_filter_locations_reads_raw_when_present():
    df = pd.DataFrame(
        [
            {"global_id": "w:1", "location": "2 Locations", "country_iso": None,
             "is_remote": None, "raw": '{"externalPath": "/job/Bengaluru/Eng"}'},
            {"global_id": "w:2", "location": "Austin, TX", "country_iso": None,
             "is_remote": None, "raw": None},
        ]
    )
    out = filter_locations(df)
    assert out["location_flag"].tolist() == ["rejected", "accepted"]
    assert out["location_reason"].tolist() == ["workday_path_foreign", "state_match"]


def test_filter_locations_works_without_a_raw_column():
    # Not every slice's DataFrame carries `raw`; row.get must not KeyError.
    df = pd.DataFrame([{"global_id": "g:1", "location": "Austin, TX"}])
    out = filter_locations(df)
    assert out["location_flag"].tolist() == ["accepted"]
