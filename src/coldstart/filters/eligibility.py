from __future__ import annotations

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
_EXCLUDE_CASE_SENSITIVE_RE = build_regex_alternation(
    _RULES["exclude_case_sensitive"], flags=0
)
_UNCERTAIN_RE = build_regex_alternation(_RULES["uncertain"])


def _raw_to_text(raw: object) -> str:
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return ""
    if isinstance(raw, dict):
        return " ".join(str(v) for v in raw.values())
    return str(raw)


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
