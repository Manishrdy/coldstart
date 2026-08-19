from __future__ import annotations

import pandas as pd

from coldstart.logging_setup import get_logger

logger = get_logger(__name__)


def _req_key_series(df: pd.DataFrame) -> pd.Series:
    # (company, requisition_id, location) — see DEVELOPMENT_PLAN.md Module 10:
    # bare requisition_id collides across unrelated companies/postings on real
    # data, so it's never used alone as a dedup key.
    return list(zip(df["company"], df["requisition_id"], df["location"], strict=True))


def _dedupe_intra_batch(df: pd.DataFrame) -> pd.DataFrame:
    df = df.reset_index(drop=True)
    has_req = df["requisition_id"].notna()

    no_req = df[~has_req]
    with_req = df[has_req].copy()
    if with_req.empty:
        return df

    # pandas' drop_duplicates treats NaN as equal to NaN, which would wrongly
    # collapse every null-requisition_id row into one — that's why they're
    # split out above and never passed through drop_duplicates at all.
    with_req["_req_key"] = _req_key_series(with_req)
    with_req["_prefers_posted_at"] = with_req["posted_at"].notna()
    with_req = with_req.sort_values("_prefers_posted_at", ascending=False, kind="stable")
    with_req = with_req.drop_duplicates(subset="_req_key", keep="first")
    with_req = with_req.drop(columns=["_req_key", "_prefers_posted_at"])

    return pd.concat([no_req, with_req]).sort_index().reset_index(drop=True)


def dedupe(
    df: pd.DataFrame, seen_global: set[str], seen_req: set[tuple[str, str, str]]
) -> pd.DataFrame:
    n_in = len(df)

    df = df[~df["global_id"].isin(seen_global)]
    n_after_global = len(df)

    if len(df):
        req_keys = pd.Series(_req_key_series(df), index=df.index)
    else:
        req_keys = pd.Series(dtype=object)
    df = df[~(df["requisition_id"].notna() & req_keys.isin(seen_req))]
    n_after_seen_req = len(df)

    df = _dedupe_intra_batch(df)
    n_out = len(df)

    logger.info(
        "in=%d, seen_global=%d, seen_req=%d, intra_batch=%d, out=%d",
        n_in,
        n_in - n_after_global,
        n_after_global - n_after_seen_req,
        n_after_seen_req - n_out,
        n_out,
    )

    return df
