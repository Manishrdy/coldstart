from __future__ import annotations

import json
import re
from pathlib import Path

_CONFIG_DIR = Path(__file__).resolve().parent.parent.parent.parent / "config"


def load_config_json(filename: str):
    return json.loads((_CONFIG_DIR / filename).read_text())


def build_word_boundary_alternation(words: list[str]) -> re.Pattern:
    escaped = sorted((re.escape(w) for w in words), key=len, reverse=True)
    return re.compile(r"\b(?:" + "|".join(escaped) + r")\b", re.IGNORECASE)


def build_regex_alternation(patterns: list[str], *, flags: int = re.IGNORECASE) -> re.Pattern:
    """Like build_word_boundary_alternation, but `patterns` are already regex
    fragments (not escaped, not auto-wrapped in \\b) — for lexicons authored
    as regex, like eligibility_rules.json's citizenship/clearance patterns."""
    return re.compile(r"(?:" + "|".join(patterns) + r")", flags)
