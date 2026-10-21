"""Pre-LLM cost filter: drop postings that can never plausibly score well.

Two independent checks, both validated against real posting data
(2026-09-02) rather than guessed: pulled every historical parquet snapshot
(2.9M raw rows across 49 ATS sources), ran the actual title/location/
eligibility filters to get the true current survivor set (19,317 rows), then
measured each rule against it — and then, critically, sampled the actual
matched JD text for every keyword rather than trusting the aggregate counts.
That second pass is what caught three real bugs before they shipped: lowercase
`.net` matching URL domains in press-release boilerplate ("c212.net/c/link"),
bare "salesforce" firing on founder bios and investor names ("co-CEO of
Salesforce", "backed by ... Salesforce Ventures") at a ~37% false-positive
rate in-sample, and "chef"/"on-call rotation" catching cafeteria-menu copy and
ordinary backend on-call duty that plenty of good-fit roles also have. All
three are fixed below; see git history for what the naive first pass caught
instead.

**Stack mismatch** — the JD names a language/stack the candidate doesn't
have (Java, .NET/C#, PHP, Ruby, Go, Rust, Kotlin, Scala, SAP/ABAP, or a
Terraform/Ansible/Puppet/SRE/Kubernetes-admin/sysadmin-only role) and never
once mentions the one they do (Python, Node.js, TypeScript, FastAPI, Flask,
LangChain, RAG, LLM). Cuts 14.9% of survivors at that bar. Deliberately
conservative: any mention of the candidate's own stack — even alongside a
mismatch keyword — passes it through to the LLM instead of guessing which
one dominates. FDE/AI-titled postings were barely touched (40 and 232 hits
respectively, out of 10k+ matches on the pre-location-filtered set), so this
isn't quietly gutting the premium role types.

**Experience floor** — not a judgment call at all. scoring/rubric.py's own
formula floors experience_fit to 15 once required years R exceeds candidate
years X by more than 4 — and at that floor the composite caps at 78.75 even
with a perfect 100 on every other dimension
(100*.35 + 15*.25 + 100*.25 + 100*.15), below score_threshold_strong (80).
A JD that states "10+ years required" is not a job the LLM will ever mark
strong today; cutting it here changes zero outcomes, only cost. Measured at
6.0% of survivors after fixing the false-positive sources above (legal-age
boilerplate — "21 years of age" — company-history bragging — "a 90+ year
history" — and misreading a range's high end instead of its floor, "5-8+
years" is a 5-year floor, not 8).

Both together: ~20.8% additional reduction in what reaches the LLM, on top
of the existing title/location/eligibility filters.

**Experience cap** (Module 39) — the operator's own line, not the rubric's:
MAX_REQUIRED_EXPERIENCE_YEARS=5 cuts anything asking for 6+ years. Unlike
the floor above this *does* change outcomes (the LLM can rate a 6-7 year
job 80+), so it was measured against the operator's decisions first: of 64
re-checkable applied jobs, none asked for 6-7 years; 35 declined ones did.
At this line a degree alternative stops being noise — "Bachelor's with 6+
years OR a Master's with 3+ years", "BS (or similar field with 6+ years)"
— so `candidate_degree` in stack_rules.json decides which path applies.
"""

from __future__ import annotations

import re

import pandas as pd

from coldstart.filters._shared import build_regex_alternation, load_config_json
from coldstart.logging_setup import get_logger

logger = get_logger(__name__)

_RULES = load_config_json("stack_rules.json")
_CANDIDATE_STACK_RE = build_regex_alternation(_RULES["candidate_stack"])
_MISMATCH_RES: dict[str, re.Pattern] = {
    name: build_regex_alternation(patterns) for name, patterns in _RULES["mismatch"].items()
}

# Finds every "<n> years"/"<n>+ yrs" token, then decides per-token whether it
# is really an experience requirement. The naive version of this (any number
# next to "years") was validated against real postings and found to catch
# two unrelated things constantly: legal-age boilerplate ("must be at least
# 21 years of age") and company-history bragging ("a 90+ year history", "40+
# years at Palantir", "30+ year track record") — neither is a candidate
# requirement, and the second is common enough (any postings from a company
# with real tenure) that treating it as one would silently exclude good
# jobs. A token only counts when "experience" shows up soon after it (the
# actual requirement phrasing: "8+ years of experience", "10+ years
# technical engineering experience") or a requirement word leads into it
# ("requires 8 years", "minimum of 10 years") — company-history sentences
# have neither. The 1-25 bound is a backstop: no individual's required years
# legitimately exceeds that, so anything past it is mis-parsed by construction.
#
# The optional leading "<n>-" group handles a range like "5-8+ years": the
# floor a candidate actually has to clear is the low end (5), not the high
# end a naive match would grab — a real posting ("Senior Applied AI/ML
# Engineer", 5-8+ years) was caught reading "8" out of that and wrongly
# excluded a candidate who clears the real 5-year floor comfortably.
_YEARS_TOKEN_RE = re.compile(
    r"(?:(\d{1,2})\s*[-–—]\s*)?(\d{1,2})\+?\s*(?:years?|yrs?)\b", re.IGNORECASE
)
_YEARS_OF_AGE_RE = re.compile(r"^\s*of\s+age\b|^\s*old\b", re.IGNORECASE)
_EXPERIENCE_NEARBY_RE = re.compile(r"exp(?:erience)?\b|required\b|minimum\b", re.IGNORECASE)
_LEADING_QUALIFIER_RE = re.compile(
    r"(?:minimum|min\.?|at least|requires?|require)\s*(?:of\s*)?$", re.IGNORECASE
)
_YEARS_LOOKAROUND_CHARS = 40
_MAX_PLAUSIBLE_YEARS = 25

# Degree alternatives — a number of years that only applies to someone
# without the candidate's degree. Both shapes come from real postings
# (torcrobotics, jobgether, doowii, 2026-10-20) that the 5-year cap would
# otherwise have cut:
#   - the bachelor's path of a graduate-degree alternative on the same line:
#     "Bachelor's degree ... with 6+ years of experience OR a Master's degree
#     with 3+ years". Requires the alternative to state its own years, so
#     "6+ years of experience; MS preferred" still counts.
#   - a substitute for the degree itself: "BS in computer engineering (or
#     similar field with 6+ years of experience)", "Bachelor's degree or 6+
#     years of equivalent experience", "6 years in lieu of a degree".
_DEGREE_RANK = {"none": 0, "bachelors": 1, "masters": 2, "phd": 3}
_CANDIDATE_DEGREE = _DEGREE_RANK[_RULES.get("candidate_degree", "none")]
_GRADUATE_DEGREE = r"(?:master|m\.s\.|\bms\b|ph\.?\s?d|doctor)"
# Only a path through a degree the candidate actually holds: for a master's,
# "…, or a Ph.D. with 3 years" is not an alternative they can take.
_HELD_GRADUATE_DEGREE = (
    r"(?:master|m\.s\.|\bms\b|ph\.?\s?d|doctor)"
    if _CANDIDATE_DEGREE >= _DEGREE_RANK["phd"]
    else r"(?:master|m\.s\.|\bms\b)"
)
# No "^" in the *_AFTER patterns: Pattern.match(text, pos) already anchors at
# pos, and "^" would only match at the very start of the text.
_GRADUATE_PATH_AFTER_RE = re.compile(
    r"[^\n]{0,120}?(?:\bor\b|[,;])\s*(?:an?\s+)?"
    + _HELD_GRADUATE_DEGREE
    + r"[^\n]{0,60}?\d{1,2}\s*\+?\s*(?:years?|yrs?)",
    re.IGNORECASE,
)
_DEGREE_SUBSTITUTE_BEFORE_RE = re.compile(
    r"\(\s*or\b[^)\n]{0,60}$"  # "(or similar field with "
    r"|(?:degree|bachelor[’']?s?|\bb\.?s\.?|\bb\.?a\.?)\s*,?\s*or\s*(?:at\s+least\s*)?$",
    re.IGNORECASE,
)
_DEGREE_SUBSTITUTE_AFTER_RE = re.compile(r"[^\n]{0,60}?in\s+lieu\s+of", re.IGNORECASE)
_GRADUATE_DEGREE_BEFORE_RE = re.compile(_GRADUATE_DEGREE + r"[^\n]{0,40}$", re.IGNORECASE)


def _is_degree_alternative(text: str, start: int, end: int) -> bool:
    if _CANDIDATE_DEGREE >= _DEGREE_RANK["masters"] and _GRADUATE_PATH_AFTER_RE.match(
        text, end
    ):
        return True
    if _CANDIDATE_DEGREE >= _DEGREE_RANK["bachelors"]:
        head = text[max(0, start - 60) : start]
        if _DEGREE_SUBSTITUTE_BEFORE_RE.search(head):
            return True
        # "…in lieu of a degree" substitutes for having one — unless the
        # years belong to a graduate path that is itself the substitute:
        # "a Master's degree and 3 years in lieu of a Bachelor's and 5".
        if _DEGREE_SUBSTITUTE_AFTER_RE.match(text, end) and not _GRADUATE_DEGREE_BEFORE_RE.search(
            head
        ):
            return True
    return False


def _text_of(description: object, raw: object) -> str:
    parts = []
    for value in (description, raw):
        if value is None or (isinstance(value, float) and pd.isna(value)):
            continue
        parts.append(str(value))
    return " ".join(parts)


def _max_required_years(text: str) -> int | None:
    years: list[int] = []
    for match in _YEARS_TOKEN_RE.finditer(text):
        tail = text[match.end() : match.end() + _YEARS_LOOKAROUND_CHARS]
        if _YEARS_OF_AGE_RE.match(tail):
            continue
        head = text[max(0, match.start() - 25) : match.start()]
        if not (_EXPERIENCE_NEARBY_RE.search(tail) or _LEADING_QUALIFIER_RE.search(head)):
            continue
        if _is_degree_alternative(text, match.start(), match.end()):
            continue
        # A range's low end ("5" in "5-8+ years") is the real floor — see
        # the pattern's docstring above.
        value = int(match.group(1)) if match.group(1) else int(match.group(2))
        if 1 <= value <= _MAX_PLAUSIBLE_YEARS:
            years.append(value)
    return max(years) if years else None


def classify_stack(
    description: object,
    raw: object,
    x_years: float,
    max_required_years: float | None = None,
) -> tuple[bool, str | None]:
    """(excluded, reason). Never excludes on missing/empty data — same
    default-trust posture as eligibility's empty-text case, since a filter
    this aggressive has to fail toward keeping a job, not dropping one.

    `max_required_years` (MAX_REQUIRED_EXPERIENCE_YEARS) can only tighten the
    rubric's X+4 floor, never loosen it."""
    text = _text_of(description, raw)
    if not text.strip():
        return False, None

    if not _CANDIDATE_STACK_RE.search(text):
        for name, pattern in _MISMATCH_RES.items():
            match = pattern.search(text)
            if match:
                return True, f"stack_mismatch:{name}:{match.group(0)}"

    years = _max_required_years(text)
    if years is not None and years > x_years + 4:
        return True, f"experience_floor:{years}yrs_required"
    if years is not None and max_required_years is not None and years > max_required_years:
        return True, f"experience_cap:{years}yrs_required"

    return False, None


def filter_stack(
    df: pd.DataFrame, x_years: float, max_required_years: float | None = None
) -> pd.DataFrame:
    df = df.copy()
    results = df.apply(
        lambda row: classify_stack(
            row.get("description"), row.get("raw"), x_years, max_required_years
        ),
        axis=1,
    )
    df["stack_excluded"] = [r[0] for r in results]
    df["stack_reason"] = [r[1] for r in results]

    total = len(df)
    excluded = int(df["stack_excluded"].sum())
    logger.info("stack filter: kept=%d excluded=%d (total=%d)", total - excluded, excluded, total)

    return df
