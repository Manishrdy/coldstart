"""Visa sponsorship: hold back postings that say they won't sponsor (Module 33).

The operator is in the US on F-1 OPT — authorised to work today, but needs an
employer to sponsor an H-1B later. A posting that says it won't do that is not
worth an application however well it scores, and at ~5 seconds a decision on
the dashboard the sentence that says so never gets read: of 118 applications
whose JD was still available, 7 went to postings that state outright they will
not sponsor (one names F-1 OPT as unsupported by name). scope.md §4.7.

Validated 2026-10-20 against the true survivor set of today's snapshots — every
in-scope posting that passes the title, company, location, eligibility and
stack filters, 13,003 rows — by reading the matched sentence for every rule,
not by trusting counts. Five rounds, each one catching something a plausible
pattern got wrong:

- **Generic "sponsor" is not visa language.** "We sponsor events", "we sponsor
  bright women from disadvantaged backgrounds", "company-sponsored employee
  benefits are not provided", "not limited to sponsoring...". The verb forms
  (sponsor/sponsored/sponsoring) only count when the same sentence talks about
  visas or work authorisation. "Sponsorship" the noun counts on its own — in a
  job posting it means a visa essentially every time.
- **Markdown hides the negation.** "does **not** provide visa support" read as
  a *yes*. Emphasis characters are stripped before matching.
- **"U.S." and "etc." end sentences.** "US work visa sponsorship (such as TN,
  O-1, H-1B, etc.) is not available" was missed until abbreviations were
  folded first.
- **Hedges from companies that do sponsor.** "We sponsor visas: H1B, O1, ...
  We can't successfully sponsor for every role" and "work authorization is
  preferred but not required, can sponsor visas" both read as refusals.
- **Real refusals are long.** "without the need for current or future
  employment-based visa sponsorship", "does not now, or in the future, require
  visa sponsorship", "we are not able to consider candidates who currently or
  in the future will require visa sponsorship". Hence the 8-word gaps.

Final numbers on that set: 5.3% say they won't sponsor (12% of workday,
under 1% of icims/gem/ycombinator), 1.1% say they will, the rest say nothing.

**Structured answers win over prose.** Some postings carry a form field —
"Sponsorship Available : Not Available", "Is role eligible for Immigration
Sponsorship?: Yes". Those are checked first and decide outright, because the
prose patterns can misfire across a field boundary with no punctuation in it
("Relocation Assistance : Not Available Sponsorship Available : Yes").

**A refusal beats an offer.** Checked in that order: "not initiating new visa
sponsorships" from a company that supports H-1B *transfers* is a refusal for
someone who needs a new petition, and so is "Will sponsor TN visa. No H1B."
H-1B transfers alone are deliberately not read as a yes, for the same reason.

**ycombinator tags its own jobs** (`raw["visa"]`), the one source that does.
"Will sponsor" is a yes; "US citizen/visa only" means already-authorised
candidates only, which for OPT is a weaker no — ranked lower, not held back;
"US citizenship/visa not required" says nothing about sponsoring and is left
neutral. The JD text still wins when it says something.
"""

from __future__ import annotations

import json
import re

import pandas as pd

from coldstart.logging_setup import get_logger
from coldstart.models import SponsorshipStance

logger = get_logger(__name__)

# --- normalisation -----------------------------------------------------------

_TAG_RE = re.compile(r"<[^>]+>")
_MARKDOWN_RE = re.compile(r"[*_#`]+")
_ABBREVIATIONS = [
    (re.compile(r"\bU\.\s?S\.(?:\s?A\.)?", re.IGNORECASE), "US"),
    (re.compile(r"\be\.g\.", re.IGNORECASE), "eg"),
    (re.compile(r"\bi\.e\.", re.IGNORECASE), "ie"),
    (re.compile(r"\betc\.", re.IGNORECASE), "etc"),
]


def _normalize(text: object) -> str:
    if not isinstance(text, str):
        return ""
    text = _TAG_RE.sub(" ", text)
    text = _MARKDOWN_RE.sub("", text).replace("’", "'")
    for pattern, replacement in _ABBREVIATIONS:
        text = pattern.sub(replacement, text)
    return re.sub(r"\s+", " ", text)


# --- the grammar -------------------------------------------------------------

# A visa or work-authorisation word somewhere in the same sentence. Required
# before a bare verb form of "sponsor" counts — see the module docstring.
_VISA_CONTEXT_RE = re.compile(
    r"visa|immigra|h-?1b|work authori|employment authori|authori[sz]ed to work|"
    r"work permit|employment-based|\bopt\b|\bcpt\b|\bf-?1\b|green card|work status|"
    r"right to work",
    re.IGNORECASE,
)

# Up to N words between a negation and the thing negated. Commas and
# parentheses are allowed inside the gap; full stops are not, so a match can
# never run from one sentence into the next.
_WORD = r"(?:[\w/()'-]+,?\s+)"
_NEGATION = (
    r"(?:not|unable to|cannot|can ?not|can't|won't|will not|don't|doesn't|aren't|"
    r"isn't|no longer)"
)
_TERM = r"(?P<term>[\w-]*sponsor\w*|(?:visa|immigration)(?:-related)?\s+support)"
_INSENSITIVE = re.IGNORECASE

# Form-field answers. Checked first and decisive — see the module docstring.
_FIELD_RE = re.compile(
    r"\b(?:visa\s+|immigration\s+|employment\s+)?sponsorship(?:\s+available)?"
    r"\s*(?:\?\s*:?|:|-|–)\s*(?P<answer>yes|no|not available|none)\b",
    _INSENSITIVE,
)
_FIELD_NO = {"no", "not available", "none"}

_WONT_SPONSOR: list[tuple[str, re.Pattern]] = [
    # "we will not sponsor", "unable to provide or continue work
    # authorization sponsorship", "does not now, or in the future, require
    # visa sponsorship"
    ("negated", re.compile(rf"\b{_NEGATION}\s+{_WORD}{{0,8}}?{_TERM}", _INSENSITIVE)),
    # "No visa sponsorship.", "Work Authorization: No Sponsorship", "No H1B."
    ("no", re.compile(rf"\bno\s+{_WORD}{{0,2}}?(?:sponsorship|h-?1b)\b", _INSENSITIVE)),
    # "authorized to work ... without the need for current or future
    # employment-based visa sponsorship". Not "with or without sponsorship",
    # which says the opposite.
    ("without", re.compile(rf"(?<!with or )\bwithout\s+{_WORD}{{0,8}}?{_TERM}", _INSENSITIVE)),
    # "Visa sponsorship is not available", "sponsorship of new applicants ...,
    # or any other immigration-related support, is not available"
    (
        "unavailable",
        re.compile(
            rf"{_TERM}[^.;?!]{{0,110}}?\b(?:is|are|will)\s+not\s+"
            r"(?:currently\s+|presently\s+)?(?:be\s+)?"
            r"(?:available|offered|provided|possible|supported|considered)\b"
            r"|\bsponsorship\s+(?:is\s+)?unavailable\b",
            _INSENSITIVE,
        ),
    ),
    # "not eligible for relocation assistance or visa sponsorship",
    # "not eligible for CPT, OPT, or visa sponsorship"
    (
        "ineligible",
        re.compile(
            rf"\b(?:not\s+eligible|ineligible)\s+for\s+{_WORD}{{0,6}}?(?:{_TERM}|opt\b|cpt\b)",
            _INSENSITIVE,
        ),
    ),
    # "Individuals ... who need sponsorship now or in the future are not
    # eligible for hire"
    (
        "need_not_eligible",
        re.compile(
            rf"\b(?:require|need)\w*\s+{_WORD}{{0,6}}?{_TERM}[^.;?!]{{0,90}}?"
            r"\b(?:are|is)\s+not\s+eligible\b",
            _INSENSITIVE,
        ),
    ),
    # "we are not able to consider candidates who currently or in the future
    # will require visa sponsorship"
    (
        "not_considered",
        re.compile(
            rf"\b{_NEGATION}\s+(?:be\s+)?(?:able\s+to\s+)?(?:consider|engage|accept)\w*\s+"
            rf"(?:any\s+)?(?:candidates|individuals|applicants|anyone)\b[^.;?!]{{0,110}}?{_TERM}",
            _INSENSITIVE,
        ),
    ),
    # "Currently, we are not accepting H-1B applicants"
    (
        "not_accepting",
        re.compile(
            r"\bnot\s+(?:\w+\s+){0,2}?(?:accepting|considering)\s+(?:\w+\s+){0,2}?"
            r"(?:h-?1b|opt|cpt|f-?1|visa)\b",
            _INSENSITIVE,
        ),
    ),
]

# A negation that is really a hedge from a company that does sponsor: "we
# can't guarantee sponsorship", "can't sponsor for every role", "preferred but
# not required, can sponsor visas". Looked for in the match and just after it.
_HEDGE_RE = re.compile(
    r"\bguarantee\w*|\bfor\s+every\b|\bevery\s+(?:role|candidate|position)|"
    r"\bnot\s+(?:be\s+)?(?:required|necessary|needed)\b",
    _INSENSITIVE,
)
_HEDGE_LOOKAHEAD_CHARS = 40

_WILL_SPONSOR: list[tuple[str, re.Pattern]] = [
    ("will_sponsor", re.compile(r"\bwill\s+sponsor\b", _INSENSITIVE)),
    (
        "available",
        re.compile(
            r"\b(?:visa\s+|h-?1b\s+|immigration\s+|employment\s+)?sponsorships?\s+"
            r"(?:is\s+|are\s+|may\s+be\s+)?(?:available|offered|provided)\b",
            _INSENSITIVE,
        ),
    ),
    (
        "we_sponsor",
        re.compile(
            r"\b(?:we|can|do|happy to|open to|able to|may|glad to)\s+(?:\w+\s+){0,2}?"
            r"sponsor\w*\s+(?:[\w/-]+,?\s+){0,6}?"
            r"(?:visas?|h-?1b|o-?1|opt|international|immigration|employment|work authori\w*)",
            _INSENSITIVE,
        ),
    ),
    (
        "open_to",
        re.compile(
            r"\bopen to (?:visa\s+)?sponsor(?:ing|ship)\b"
            r"|\bsupports?\s+visa\s+sponsorship\b"
            r"|\b(?:is|are|be)\s+eligible\s+for\s+(?:visa\s+|immigration\s+)?sponsorship\b"
            r"|\bconsider\w*\s+(?:visa\s+)?sponsor(?:ing|ship)\b"
            r"|\bsponsors?\s+(?:\w+\s+){0,2}?(?:visas|h-?1b)\b"
            r"|\b(?:offer|provide)s?\s+(?:visa|h-?1b|immigration)\s+(?:sponsorship|support)\b",
            _INSENSITIVE,
        ),
    ),
    # A benefits bullet: "- Visa sponsorship", "Full visa sponsorship and
    # relocation support". Last, and only reached when no refusal matched.
    # O-1/J-1-only offers are not the visa an OPT candidate needs.
    (
        "listed_benefit",
        re.compile(
            r"(?<!o-1 )(?<!o1 )(?<!j-1 )\b(?:full\s+)?visa\s+"
            r"(?:&\s+permanent\s+residence\s+)?sponsorships?\b",
            _INSENSITIVE,
        ),
    ),
]
# Verb forms that need visa context to count; see the module docstring.
_NEEDS_CONTEXT = {"will_sponsor"}

_REASON_MAX_CHARS = 80

# ycombinator's own tag, the only structured sponsorship field any in-scope
# source ships (checked 2026-10-20 across all ten).
_YC_VISA_KEY = "visa"
_YC_VISA_STANCES = {
    "will sponsor": SponsorshipStance.WILL_SPONSOR,
    "us citizen/visa only": SponsorshipStance.EXISTING_AUTHORIZATION_ONLY,
}


# --- classification ----------------------------------------------------------


def _sentence_around(text: str, start: int, end: int) -> str:
    left = max(text.rfind(mark, 0, start) for mark in (". ", "; ", "? ", "! "))
    right_candidates = [i for i in (text.find(m, end) for m in (". ", "; ", "? ", "! ")) if i >= 0]
    right = min(right_candidates) if right_candidates else len(text)
    return text[left + 1 if left >= 0 else 0 : right]


def _is_generic_term(term: str | None) -> bool:
    """A verb form ("sponsor", "employer-sponsored") rather than the noun
    "sponsorship" or a "visa support" phrase."""
    if not term:
        return False
    term = term.lower()
    return "sponsorship" not in term and not term.endswith("support")


def _reason(rule: str, match: re.Match) -> str:
    return f"{rule}:{match.group(0)[:_REASON_MAX_CHARS]}"


def _refusal(text: str) -> str | None:
    for rule, pattern in _WONT_SPONSOR:
        for match in pattern.finditer(text):
            window = text[match.start() : match.end() + _HEDGE_LOOKAHEAD_CHARS]
            if _HEDGE_RE.search(window):
                continue
            term = match.groupdict().get("term")
            if _is_generic_term(term) and not _VISA_CONTEXT_RE.search(
                _sentence_around(text, match.start(), match.end())
            ):
                continue
            return _reason(rule, match)
    return None


def _offer(text: str) -> str | None:
    for rule, pattern in _WILL_SPONSOR:
        for match in pattern.finditer(text):
            if rule in _NEEDS_CONTEXT and not _VISA_CONTEXT_RE.search(
                _sentence_around(text, match.start(), match.end())
            ):
                continue
            return _reason(rule, match)
    return None


def _yc_visa_tag(raw: object) -> str | None:
    if raw is None or (isinstance(raw, float) and pd.isna(raw)):
        return None
    if isinstance(raw, dict):
        value = raw.get(_YC_VISA_KEY)
        return str(value) if value else None
    text = str(raw)
    # Only pay for the parse when the key is there at all.
    if f'"{_YC_VISA_KEY}"' not in text:
        return None
    try:
        parsed = json.loads(text)
    except ValueError:
        return None
    if not isinstance(parsed, dict):
        return None
    value = parsed.get(_YC_VISA_KEY)
    return str(value) if value else None


def classify_sponsorship(
    description: object, raw: object
) -> tuple[SponsorshipStance | None, str | None]:
    """(stance, reason). None/None when the posting says nothing, which is
    most of them — never a guess in either direction."""
    text = _normalize(description)

    if text:
        field = _FIELD_RE.search(text)
        if field:
            stance = (
                SponsorshipStance.WONT_SPONSOR
                if field.group("answer").lower() in _FIELD_NO
                else SponsorshipStance.WILL_SPONSOR
            )
            return stance, _reason("field", field)

        if (reason := _refusal(text)) is not None:
            return SponsorshipStance.WONT_SPONSOR, reason
        if (reason := _offer(text)) is not None:
            return SponsorshipStance.WILL_SPONSOR, reason

    tag = _yc_visa_tag(raw)
    if tag is not None:
        stance = _YC_VISA_STANCES.get(tag.strip().lower())
        if stance is not None:
            return stance, f"yc_visa:{tag}"

    return None, None


def filter_sponsorship(df: pd.DataFrame, *, enabled: bool) -> pd.DataFrame:
    """Adds `sponsorship`, `sponsorship_reason` and `sponsorship_excluded`.

    The stance is recorded on every row either way — it is a ranking signal
    too — but only `enabled` (NEEDS_VISA_SPONSORSHIP) turns a refusal into a
    hold-back. Someone who doesn't need a visa loses nothing to this filter."""
    df = df.copy()
    descriptions = df["description"] if "description" in df.columns else [None] * len(df)
    raws = df["raw"] if "raw" in df.columns else [None] * len(df)
    results = [classify_sponsorship(d, r) for d, r in zip(descriptions, raws, strict=True)]

    # object dtype, so an unstated stance stays None — pandas would otherwise
    # infer a string column and turn every None into NaN.
    df["sponsorship"] = pd.Series(
        [stance.value if stance else None for stance, _ in results], index=df.index, dtype=object
    )
    df["sponsorship_reason"] = pd.Series(
        [reason for _, reason in results], index=df.index, dtype=object
    )
    df["sponsorship_excluded"] = [
        enabled and stance is SponsorshipStance.WONT_SPONSOR for stance, _ in results
    ]

    counts = pd.Series(df["sponsorship"], dtype=object).fillna("unstated").value_counts()
    logger.info(
        "sponsorship filter: %s, excluded=%d (enabled=%s, total=%d)",
        counts.to_dict(),
        int(sum(df["sponsorship_excluded"])),
        enabled,
        len(df),
    )
    return df
