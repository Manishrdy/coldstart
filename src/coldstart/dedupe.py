from __future__ import annotations

import pandas as pd

from coldstart.logging_setup import get_logger

logger = get_logger(__name__)


def _req_key_series(df: pd.DataFrame) -> pd.Series:
    # (company, requisition_id, location) — see DEVELOPMENT_PLAN.md Module 10:
    # bare requisition_id collides across unrelated companies/postings on real
    # data, so it's never used alone as a dedup key.
    return list(zip(df["company"], df["requisition_id"], df["location"], strict=True))


def _prefer_dated_first(df: pd.DataFrame) -> pd.DataFrame:
    return df.assign(_dated=df["posted_at"].notna()).sort_values(
        "_dated", ascending=False, kind="stable"
    ).drop(columns="_dated")


def _dedupe_same_key(df: pd.DataFrame) -> pd.DataFrame:
    # One row per global_id. Without this, two postings sharing a key in one
    # batch both reach the LLM and the second upsert overwrites the first's
    # score — two calls, one answer kept. With global_ids.py's per-employer
    # keys what's left sharing a key is one employer's opening listed on two
    # of its career sites, or the 106 keys load_slice warns about.
    # DEVELOPMENT_PLAN.md Module 37.
    df = df.reset_index(drop=True)
    kept = _prefer_dated_first(df).drop_duplicates(subset="global_id", keep="first")
    return kept.sort_index().reset_index(drop=True)


def _dedupe_intra_batch(df: pd.DataFrame) -> pd.DataFrame:
    df = _dedupe_same_key(df)
    has_req = df["requisition_id"].notna()

    no_req = df[~has_req]
    with_req = df[has_req].copy()
    if with_req.empty:
        return df

    # pandas' drop_duplicates treats NaN as equal to NaN, which would wrongly
    # collapse every null-requisition_id row into one — that's why they're
    # split out above and never passed through drop_duplicates at all.
    with_req["_req_key"] = _req_key_series(with_req)
    with_req = _prefer_dated_first(with_req)
    with_req = with_req.drop_duplicates(subset="_req_key", keep="first")
    with_req = with_req.drop(columns=["_req_key"])

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
