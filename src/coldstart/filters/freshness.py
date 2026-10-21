"""Drop postings that are too old to be worth scoring.

A posting that went up months ago is usually filled, and paying an LLM to
score it is money spent on something you can't apply to. This is the single
most effective cost lever in the pipeline: measured across greenhouse, lever
and ashby, 10,448 of the 10,969 jobs that reach the LLM today are more than
15 days old — a 15-day cutoff removes ~95% of the spend.

**Jobs with no posted date are kept**, deliberately. The source data omits it
often enough that treating "unknown" as "old" would silently discard real
opportunities, which is the §10 failure mode this project is built to avoid.
An absent date is not evidence of staleness.

One caveat worth understanding, because it can bite: this measures age
against *today*, not against the snapshot. The upstream manifest is
regenerated irregularly — it sat unchanged for 13 days at time of writing —
so the freshest posting in a slice is already as old as the snapshot itself.
If upstream ever goes quiet for longer than `MAX_POSTING_AGE_DAYS`, this
filter will correctly but unhelpfully drop *everything*. `filter_freshness`
logs a WARNING when it drops a whole batch, so that shows up as a signal
rather than as silence.
"""

from __future__ import annotations

from datetime import UTC, datetime

import pandas as pd

from coldstart.logging_setup import get_logger

logger = get_logger(__name__)


def now_utc() -> pd.Timestamp:
    """The current time, tz-aware UTC.

    Deliberately routed through `datetime.now` rather than the more obvious
    `pd.Timestamp.now(tz="UTC")`. `Timestamp.now` is implemented in C, so
    freezegun cannot patch it: any test that freezes the clock would silently
    get the *real* date here and quietly drift into meaninglessness as the
    calendar moves. That is not hypothetical — it rotted two tests once
    already. Please don't "simplify" this back."""
    return pd.Timestamp(datetime.now(UTC))


def posting_age_days(posted_at: object, now: pd.Timestamp) -> float | None:
    """Age in days, or None when there's no usable date.

    Handles the real mix in the data: tz-aware strings
    (`2026-06-03T19:34:14+00:00`), naive ones (`2026-04-03T00:00:00`), real
    timestamps, and NaT/None. Naive values are read as UTC."""
    stamp = pd.to_datetime(posted_at, errors="coerce", utc=True)
    if stamp is None or pd.isna(stamp):
        return None
    return (now - stamp).total_seconds() / 86400.0


def is_fresh(posted_at: object, now: pd.Timestamp, max_age_days: int) -> tuple[bool, str]:
    """(keep, reason). Never raises — real parquet data is messy."""
    age = posting_age_days(posted_at, now)
    if age is None:
        return True, "no_posted_date"
    if age < 0:
        # Dated in the future. A data quirk, not a reason to discard a job.
        return True, "future_posted_date"
    if age > max_age_days:
        return False, f"stale_{int(age)}d"
    return True, "fresh"


def filter_freshness(
    df: pd.DataFrame, max_age_days: int, *, now: pd.Timestamp | None = None
) -> pd.DataFrame:
    """Drop postings older than `max_age_days`.

    Dropped, not persisted — same treatment as the title and location
    filters, and for the same reason: the volume is enormous (~95% of what
    reaches this point) and a row per stale posting would bloat the database
    without telling anyone anything."""
    if "posted_at" not in df.columns or df.empty:
        return df

    now = now if now is not None else now_utc()
    # format="mixed" is load-bearing. Without it pandas infers ONE format from
    # the first value and, under errors="coerce", silently turns every value
    # written differently into NaT — and NaT is "undated", which is kept. A
    # slice whose first posted_at was tz-aware let every naive-dated posting
    # through regardless of age: postings 3,600+ days old reached the LLM and
    # the dashboard. "mixed" parses each value on its own, exactly as the
    # scalar `posting_age_days` does, so the two can't disagree.
    parsed = pd.to_datetime(df["posted_at"], errors="coerce", utc=True, format="mixed")
    ages = (now - parsed).dt.total_seconds() / 86400.0

    # NaT -> NaN -> comparison is False -> kept. Explicit for the reader.
    stale_mask = ages.notna() & (ages > max_age_days)

    total = len(df)
    dropped = int(stale_mask.sum())
    undated = int(ages.isna().sum())
    logger.info(
        "freshness filter: kept=%d dropped=%d undated_kept=%d (total=%d, max_age=%dd)",
        total - dropped,
        dropped,
        undated,
        total,
        max_age_days,
    )

    if total and dropped == total:
        logger.warning(
            "freshness filter dropped the entire batch of %d — every posting is older than "
            "%d days. Expected if upstream hasn't regenerated recently; raise "
            "MAX_POSTING_AGE_DAYS if this persists.",
            total,
            max_age_days,
        )

    return df[~stale_mask].reset_index(drop=True)
