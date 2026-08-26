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

# Per scope.md §4.2: seniority is *mostly* a scoring penalty (Module 14), not
# a filter-stage exclusion — that's still true for senior/principal. "staff"
# and "lead" are the deliberate exception (2026-08-25, explicit request):
# both are hard-denied in config/title_rules.json's deny list.
#
# "staff" as a bare word collides with "Member of Technical Staff" (and its
# "MTS" abbreviation) — a distinct senior IC title at AI labs, not a
# "Staff <role>" seniority prefix, so the word "staff" there names the role
# rather than modifying it. Checked before the deny list, same shape as
# location.py's rescue-signal pattern, and short-circuits straight to a keep
# — MTS titles rarely contain "engineer", so they wouldn't survive the
# ordinary allow-list check either.
_MTS_RESCUE_RE = re.compile(r"\bmember of technical staff\b|\bmts\b", re.IGNORECASE)


def is_target_title(title: str | None) -> tuple[bool, str]:
    if title is None or (isinstance(title, float) and pd.isna(title)) or not str(title).strip():
        return False, "empty_title"

    text = str(title)

    if _MTS_RESCUE_RE.search(text):
        return True, "mts_rescue"

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
