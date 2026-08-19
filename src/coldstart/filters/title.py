from __future__ import annotations

import re

import pandas as pd

from coldstart.filters._shared import build_word_boundary_alternation, load_config_json
from coldstart.logging_setup import get_logger

logger = get_logger(__name__)

_RULES = load_config_json("title_rules.json")
_ALLOW_RE = build_word_boundary_alternation(_RULES["allow"])
_DENY_RE = build_word_boundary_alternation(_RULES["deny"])

# "SE" alone is too ambiguous to allow-list as a bare word-boundary match
# (unlike SWE/SDE/FDE, it collides too easily) — require it be immediately
# followed by a level indicator, e.g. "SE II" / "SE 2" (DEVELOPMENT_PLAN.md
# Module 8).
_SE_LEVEL_RE = re.compile(r"\bSE\s+(?:I{1,3}|IV|V|\d+)\b", re.IGNORECASE)

# Per scope.md §4.2: seniority (senior/staff/principal/lead) is a scoring
# penalty (Module 14), never a filter-stage exclusion. Do NOT add these to
# config/title_rules.json's deny list — a "fix" that denies on seniority
# would silently break the "reach roles are still worth seeing" design.


def is_target_title(title: str | None) -> tuple[bool, str]:
    if title is None or (isinstance(title, float) and pd.isna(title)) or not str(title).strip():
        return False, "empty_title"

    text = str(title)

    if _DENY_RE.search(text):
        return False, "deny_match"

    if _ALLOW_RE.search(text) or _SE_LEVEL_RE.search(text):
        return True, "allow_match"

    return False, "no_match"


def filter_titles(df: pd.DataFrame) -> pd.DataFrame:
    decisions = df["title"].apply(is_target_title)
    keep_mask = decisions.apply(lambda d: d[0])

    for title, (keep, reason) in zip(df["title"], decisions, strict=True):
        logger.debug("title: %r -> %s (%s)", title, "keep" if keep else "drop", reason)

    total = len(df)
    kept = int(keep_mask.sum())
    dropped = total - kept
    logger.info("title filter: kept=%d dropped=%d (total=%d)", kept, dropped, total)

    if total and dropped / total > 0.99:
        logger.warning(
            "title filter dropped %.1f%% of the batch (%d/%d) — check for a broken regex",
            dropped / total * 100,
            dropped,
            total,
        )

    return df[keep_mask].reset_index(drop=True)
