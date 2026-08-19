import json
from pathlib import Path

import pandas as pd
import pytest

from coldstart.filters.title import filter_titles, is_target_title

FIXTURES_PATH = Path(__file__).parent / "fixtures" / "titles.json"
CASES = json.loads(FIXTURES_PATH.read_text())


@pytest.mark.parametrize("case", CASES, ids=[c["note"] for c in CASES])
def test_title_fixture_cases(case):
    keep, reason = is_target_title(case["input"])
    assert keep == case["expected_keep"], (
        f"input={case['input']!r} note={case['note']!r} got reason={reason!r}"
    )


def test_is_target_title_never_raises_on_none():
    keep, reason = is_target_title(None)
    assert keep is False
    assert reason == "empty_title"


def test_is_target_title_never_raises_on_nan():
    keep, reason = is_target_title(float("nan"))
    assert keep is False
    assert reason == "empty_title"


def test_filter_titles_drops_non_matching_rows():
    df = pd.DataFrame(
        {
            "global_id": ["a", "b", "c"],
            "title": ["Software Engineer", "Engineering Manager", "AI Engineer"],
        }
    )
    result = filter_titles(df)
    assert list(result["global_id"]) == ["a", "c"]


def test_filter_titles_resets_index():
    df = pd.DataFrame(
        {"title": ["Engineering Manager", "Software Engineer", "Recruiter", "SWE"]}
    )
    result = filter_titles(df)
    assert list(result.index) == list(range(len(result)))


def test_filter_titles_empty_dataframe():
    df = pd.DataFrame({"title": []})
    result = filter_titles(df)
    assert len(result) == 0


def test_filter_titles_warns_when_nearly_everything_dropped(caplog):
    import logging

    df = pd.DataFrame({"title": ["Manager"] * 100 + ["Software Engineer"]})
    with caplog.at_level(logging.WARNING, logger="coldstart.filters.title"):
        result = filter_titles(df)
    assert len(result) == 1
    assert any("broken regex" in r.getMessage() for r in caplog.records)


def test_filter_titles_no_warning_under_threshold(caplog):
    import logging

    df = pd.DataFrame({"title": ["Software Engineer"] * 50 + ["Manager"] * 10})
    with caplog.at_level(logging.WARNING, logger="coldstart.filters.title"):
        filter_titles(df)
    assert not any("broken regex" in r.getMessage() for r in caplog.records)
