"""Company-level block list.

`excluded_ats.json` (Module 5) already stops these employers' own ATS feeds
from ever being downloaded, which is a hard guarantee for the common case.
This is the second gate, for the case that exclusion structurally cannot
cover: the same employer posting through *someone else's* platform. Verified
on real data — `amazon.jobs.personio.com` carries Amazon postings on the
`personio` slice, which the source exclusion never sees.

The requirement is absolute: a blocked company is never scored and its job
descriptions are never sent to an LLM. So this runs immediately after the
title filter, before location, eligibility, routing, or scoring.

**Matching is exact on the normalized name, never a substring**, because
substring matching here would cost real opportunities. Real values in the
data that must NOT be blocked: `apple-roofing` (58 postings, a roofing
company), `Meta House`, `Meta Group`, `meta-agility`, `meta-five`,
`meta-bot`. Hostname-shaped values are matched on their first DNS label, so
`amazon.jobs.personio.com` is caught while `apple-roofing` is not.
"""

from __future__ import annotations

import re

import pandas as pd

from coldstart.filters._shared import load_config_json
from coldstart.logging_setup import get_logger

logger = get_logger(__name__)

def _entry_forms(name: str) -> set[str]:
    """A block-list entry matches both its spaced and its run-together form,
    so one `"uber freight"` entry covers `Uber Freight` and `uberfreight`
    (which is how it actually appears on the greenhouse slice)."""
    tokens = [t for t in _TOKENS.split(name.strip().lower()) if t]
    return {" ".join(tokens), "".join(tokens)} - {""}


_EXCLUDED: set[str] = set()

_HOSTNAME = re.compile(r"^[a-z0-9\-]+(?:\.[a-z0-9\-]+)+$")

# Trailing tokens that carry no identity — stripped only from the END of a
# name, never from the middle. Kept deliberately short: every entry here is a
# chance to over-match. "group" and "house" are absent on purpose, because
# "Meta Group" and "Meta House" are real unrelated companies in the data and
# stripping those words would block both.
_SUFFIX_TOKENS = frozenset(
    {
        "inc", "llc", "ltd", "limited", "corp", "corporation", "co", "com",
        "company", "plc", "gmbh", "ag", "sa", "nv", "bv", "pty",
        "platforms", "technologies", "technology",
    }
)

_TOKENS = re.compile(r"[^a-z0-9]+")

for _entry in load_config_json("excluded_companies.json"):
    _EXCLUDED |= _entry_forms(_entry["name"])


def _normalize(text: str) -> str:
    return " ".join(t for t in _TOKENS.split(text) if t)


def _candidates(company: str) -> set[str]:
    """Every form of `company` that may legitimately be compared to the list."""
    text = company.strip().lower()
    if not text:
        return set()

    forms = {text, _normalize(text), _normalize(text).replace(" ", "")}

    # `amazon.jobs.personio.com` -> `amazon`. Split on dots only, so a
    # hyphenated first label survives intact: `apple-roofing.breezy.hr` yields
    # `apple-roofing`, which is correctly not a match.
    if _HOSTNAME.match(text):
        label = text.split(".", 1)[0]
        forms.add(label)
        forms.add(_normalize(label))

    # "Meta Platforms, Inc." -> "meta". Only trailing descriptor tokens go;
    # a distinguishing word is never dropped, so "Apple Roofing" keeps its
    # "roofing" and stays out of the block list.
    tokens = _normalize(text).split()
    while tokens and tokens[-1] in _SUFFIX_TOKENS:
        tokens.pop()
    if tokens:
        forms.add(" ".join(tokens))
        forms.add("".join(tokens))

    forms.discard("")
    return forms


def is_excluded_company(company: object) -> tuple[bool, str]:
    """(excluded, matched_name). Never raises — real data is messy."""
    if company is None or not isinstance(company, str):
        return False, ""
    for form in _candidates(company):
        if form in _EXCLUDED:
            return True, form
    return False, ""


def filter_companies(df: pd.DataFrame) -> pd.DataFrame:
    """Drop every row whose company is blocked.

    Dropped, not persisted — consistent with how `excluded_ats.json` skips
    whole slices without writing rows. These are a standing policy decision,
    not a per-job judgement worth an audit record. Each drop is logged at
    WARNING so the block is visible without opening the database."""
    if "company" not in df.columns:
        return df

    decisions = df["company"].apply(is_excluded_company)
    drop_mask = decisions.apply(lambda d: d[0])
    dropped = int(drop_mask.sum())

    if dropped:
        for company, (excluded, matched) in zip(df["company"], decisions, strict=True):
            if excluded:
                logger.warning(
                    "company excluded: %r matched block-list entry %r — not scored",
                    company,
                    matched,
                )

    logger.info(
        "company filter: kept=%d dropped=%d (total=%d)", len(df) - dropped, dropped, len(df)
    )
    return df[~drop_mask].reset_index(drop=True)
