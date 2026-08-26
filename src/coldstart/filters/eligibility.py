from __future__ import annotations

import json
import re

import pandas as pd

from coldstart.filters._shared import build_regex_alternation, load_config_json
from coldstart.logging_setup import get_logger
from coldstart.models import EligibilityFlag

logger = get_logger(__name__)

_RULES = load_config_json("eligibility_rules.json")

_EXCLUDE_RE = build_regex_alternation(_RULES["exclude"])
# EAR (Export Administration Regulations) is checked case-sensitively, split
# out from the main case-insensitive exclude list — lowercase "ear" is an
# ordinary English word and a case-insensitive \bEAR\b would be a real false-
# positive risk here, unlike ITAR/TS-SCI which aren't real words either way.
_EXCLUDE_CASE_SENSITIVE_RE = build_regex_alternation(_RULES["exclude_case_sensitive"], flags=0)
_UNCERTAIN_RE = build_regex_alternation(_RULES["uncertain"])


# `raw` is free text as far as the exclude patterns are concerned, with one
# exception: some sources ship a structured sponsorship stance in it, and a
# stance is not a citizenship bar. ycombinator's `visa` key is the live case —
# every one of its 3,419 rows carries one, and its three values are
# "Will sponsor", "US citizen/visa only" and "US citizenship/visa not
# required". Scanned as text, the last two both match the `u.s. citizen` /
# `us citizenship` exclude patterns — including the one that says citizenship
# is *not* required, because the phrase is a substring of its own negation.
# That contradicts §4.3, which is explicit that "no visa sponsorship" is not
# an exclusion, so the field is dropped before the scan rather than read.
#
# Dropped, not interpreted: "will this employer sponsor" is a scoring signal,
# not an eligibility gate, and nothing downstream consumes it yet. Everything
# else in `raw` is still scanned exactly as before, and a genuine bar stated
# in the description ("must hold an active TS/SCI") is untouched by this —
# it never lived in `raw` to begin with.
_SPONSORSHIP_KEYS = frozenset({"visa"})

# Matches the key/value pair inside a JSON *string* raw, which is the shape
# every source actually ships (mercor, ycombinator and the rest are all
# `str`, never `dict`). The dict branch below is kept for callers that pass
# one directly, tests included.
_SPONSORSHIP_JSON_RE = re.compile(
    r'"(?:' + "|".join(sorted(_SPONSORSHIP_KEYS)) + r')"\s*:\s*"[^"]*"',
    re.IGNORECASE,
)


def _raw_to_text(raw: object) -> str:
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return ""
    if isinstance(raw, dict):
        return " ".join(
            str(value) for key, value in raw.items() if str(key).lower() not in _SPONSORSHIP_KEYS
        )

    text = str(raw)
    # Only pay for the parse when the key is actually present — this runs per
    # row over slices in the hundreds of thousands.
    if not _SPONSORSHIP_JSON_RE.search(text):
        return text

    try:
        parsed = json.loads(text)
    except ValueError:
        # Not valid JSON after all (a description that merely quotes the
        # word). Strip the pair textually rather than scanning it.
        return _SPONSORSHIP_JSON_RE.sub("", text)

    if isinstance(parsed, dict):
        return _raw_to_text(parsed)
    return text


def _clean_str(value: object) -> str:
    if value is None or (isinstance(value, float) and pd.isna(value)):
        return ""
    return str(value)


def classify_eligibility(
    description: str | None, raw: dict | str | None
) -> tuple[EligibilityFlag, str]:
    text = (_clean_str(description) + " " + _raw_to_text(raw)).strip()

    if not text:
        return EligibilityFlag.UNCERTAIN, "no_description_data"

    match = _EXCLUDE_RE.search(text)
    if match:
        return EligibilityFlag.EXCLUDED, match.group(0)

    match = _EXCLUDE_CASE_SENSITIVE_RE.search(text)
    if match:
        return EligibilityFlag.EXCLUDED, match.group(0)

    match = _UNCERTAIN_RE.search(text)
    if match:
        return EligibilityFlag.UNCERTAIN, match.group(0)

    return EligibilityFlag.PASSED, "no_match"


def filter_eligibility(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    results = df.apply(
        lambda row: classify_eligibility(row.get("description"), row.get("raw")),
        axis=1,
    )
    df["eligibility_flag"] = [r[0].value for r in results]
    df["eligibility_reason"] = [r[1] for r in results]

    global_ids = df["global_id"] if "global_id" in df.columns else ["unknown"] * len(df)
    for global_id, flag, reason in zip(
        global_ids, df["eligibility_flag"], df["eligibility_reason"], strict=True
    ):
        if flag == EligibilityFlag.EXCLUDED.value:
            logger.warning("eligibility EXCLUDED: %s (matched %r)", global_id, reason)
        else:
            logger.debug("eligibility: %s -> %s (%s)", global_id, flag, reason)

    counts = df["eligibility_flag"].value_counts().to_dict()
    logger.info("eligibility filter: %s (total=%d)", counts, len(df))

    return df
