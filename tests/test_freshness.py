from __future__ import annotations

import pandas as pd
import pytest
from freezegun import freeze_time

from coldstart.filters.freshness import filter_freshness, is_fresh, posting_age_days

NOW = pd.Timestamp("2026-08-20T12:00:00", tz="UTC")


# --- age arithmetic --------------------------------------------------------


@pytest.mark.parametrize(
    "posted,expected_days",
    [
        ("2026-08-20T12:00:00+00:00", 0),
        ("2026-08-19T12:00:00+00:00", 1),
        ("2026-08-05T12:00:00+00:00", 15),
        ("2026-07-20T12:00:00+00:00", 31),
    ],
)
def test_age_is_measured_in_whole_days(posted, expected_days):
    assert round(posting_age_days(posted, NOW)) == expected_days


def test_naive_timestamps_are_read_as_utc():
    """Real data mixes the two — `2026-04-03T00:00:00` alongside
    `2026-06-03T19:34:14.308000+00:00`."""
    naive = posting_age_days("2026-08-10T12:00:00", NOW)
    aware = posting_age_days("2026-08-10T12:00:00+00:00", NOW)
    assert naive == aware == 10


@pytest.mark.parametrize("value", [None, "", "not a date", float("nan"), pd.NaT])
def test_unparseable_dates_have_no_age(value):
    assert posting_age_days(value, NOW) is None


# --- the keep/drop decision ------------------------------------------------


def test_a_posting_inside_the_window_is_kept():
    assert is_fresh("2026-08-10T00:00:00+00:00", NOW, 15) == (True, "fresh")


def test_a_posting_outside_the_window_is_dropped():
    keep, reason = is_fresh("2026-06-01T00:00:00+00:00", NOW, 15)
    assert keep is False
    assert reason.startswith("stale_")


def test_the_boundary_day_is_kept_not_dropped():
    """Exactly `max_age_days` old still counts as fresh — the cutoff is
    'older than', so a job doesn't vanish the moment it turns 15."""
    assert is_fresh("2026-08-05T12:00:00+00:00", NOW, 15)[0] is True
    assert is_fresh("2026-08-05T11:59:00+00:00", NOW, 15)[0] is False


def test_a_posting_with_no_date_is_always_kept():
    """The user's rule is conditional on having a date, and treating unknown
    as old would silently discard real opportunities."""
    for value in (None, "", float("nan"), pd.NaT):
        assert is_fresh(value, NOW, 15) == (True, "no_posted_date")


def test_a_future_posted_date_is_kept():
    assert is_fresh("2026-09-01T00:00:00+00:00", NOW, 15) == (True, "future_posted_date")


# --- the dataframe filter --------------------------------------------------


def _frame(dates):
    return pd.DataFrame({"posted_at": dates, "title": ["Software Engineer"] * len(dates)})


def test_filter_keeps_fresh_and_undated_and_drops_stale():
    df = _frame(
        [
            "2026-08-19T00:00:00+00:00",  # 1 day
            "2026-06-01T00:00:00+00:00",  # 80 days
            None,                          # no date
            "2026-08-18T00:00:00",        # naive, 2 days
            "2025-10-30T00:00:00+00:00",  # very old
        ]
    )
    out = filter_freshness(df, 15, now=NOW)
    assert len(out) == 3
    assert list(out.index) == [0, 1, 2]  # reindexed for the downstream filters


def test_filter_warns_when_it_drops_the_whole_batch(caplog):
    """The failure mode that would otherwise be silent: if upstream stops
    regenerating for longer than the window, everything is stale and the
    pipeline quietly does nothing."""
    df = _frame(["2026-01-01T00:00:00+00:00", "2026-02-01T00:00:00+00:00"])
    with caplog.at_level("WARNING"):
        out = filter_freshness(df, 15, now=NOW)
    assert len(out) == 0
    assert "dropped the entire batch" in caplog.text


def test_filter_does_not_warn_when_something_survives(caplog):
    df = _frame(["2026-08-19T00:00:00+00:00", "2026-01-01T00:00:00+00:00"])
    with caplog.at_level("WARNING"):
        filter_freshness(df, 15, now=NOW)
    assert "dropped the entire batch" not in caplog.text


def test_filter_tolerates_an_empty_frame_and_a_missing_column():
    assert len(filter_freshness(_frame([]), 15, now=NOW)) == 0
    df = pd.DataFrame({"title": ["Software Engineer"]})
    assert len(filter_freshness(df, 15, now=NOW)) == 1


def test_a_wider_window_keeps_more():
    df = _frame(["2026-08-19T00:00:00+00:00", "2026-07-20T00:00:00+00:00"])
    assert len(filter_freshness(df, 15, now=NOW)) == 1
    assert len(filter_freshness(df, 60, now=NOW)) == 2


@freeze_time("2019-01-15T12:00:00Z")
def test_now_defaults_to_the_current_time():
    """Omitting `now` must use the *patchable* current time.

    The dates are deliberately far in the real past and anchored only to the
    frozen clock, never to whenever this test was written. Against the frozen
    clock the first posting is one day old and survives; against any real
    clock both are years stale and the frame comes back empty. So this fails
    loudly if the default ever reverts to a clock freezegun can't patch (see
    `now_utc`), and it can't rot as real time passes."""
    df = _frame(["2019-01-14T00:00:00+00:00", "2018-10-01T00:00:00+00:00"])
    out = filter_freshness(df, 15)
    assert out["posted_at"].tolist() == ["2019-01-14T00:00:00+00:00"]


def test_filter_drops_stale_postings_written_in_a_different_format_from_the_first():
    """Regression: pandas infers one format from the first value and, under
    errors="coerce", turns every differently-written value into NaT — which
    reads as undated and is kept. A slice that opened with a tz-aware date
    let naive-dated postings from 2016 straight through to the LLM."""
    df = _frame(
        [
            "2026-08-19T00:00:00+00:00",         # 1 day, tz-aware — sets the format
            "2016-11-27T00:00:00",               # ~10 years, naive
            "2023-06-02",                        # date only
            "2026-06-03T19:34:14.308000+00:00",  # fractional seconds
            "2026-08-18T00:00:00",               # 2 days, naive — kept
            None,                                # undated — kept
        ]
    )
    out = filter_freshness(df, 15, now=NOW)
    assert out["posted_at"].tolist()[:2] == ["2026-08-19T00:00:00+00:00", "2026-08-18T00:00:00"]
    assert len(out) == 3 and pd.isna(out["posted_at"].iloc[2])


def test_filter_agrees_with_is_fresh_row_by_row():
    """The frame filter and the scalar check are two code paths over one
    rule; db.stale_jobs prunes with the scalar one. They must never
    disagree about a value."""
    dates = [
        "2026-08-19T00:00:00+00:00",
        "2016-11-27T00:00:00",
        "2023-06-02",
        "2026-08-18 06:00:00",
        "2026-06-03T19:34:14.308000+00:00",
        pd.Timestamp("2026-08-01"),
        None,
        "not a date",
    ]
    kept = filter_freshness(_frame(dates), 15, now=NOW)["posted_at"].tolist()
    assert kept == [d for d in dates if is_fresh(d, NOW, 15)[0]]
