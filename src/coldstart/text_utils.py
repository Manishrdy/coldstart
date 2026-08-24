from __future__ import annotations

import re

_HEADER_LINE_RE = re.compile(r"^[A-Z][A-Z\s&/]*[A-Z]$")
# Substrings, not exact names, so this generalizes past this one resume's
# exact wording (e.g. "TECHNICAL SKILLS", "PROJECTS" both still match).
_KEEP_SECTION_KEYWORDS = ("experience", "project", "skill")
# Per-project repo links (github.com/<username>/<repo>) leak the same
# identity a profile link would — the contact-line link is already dropped
# structurally (it's before the first section header), but these live
# inside the kept "project" section itself, so they need their own pass.
_GITHUB_LINKEDIN_URL_RE = re.compile(r"\b(?:github\.com|linkedin\.com)\S*", re.IGNORECASE)


def strip_markdown_json_fences(text: str) -> str:
    """Strip a ```json ... ``` or ``` ... ``` wrapper some LLMs add around
    JSON output despite being told not to."""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()
    return cleaned


def _looks_like_section_header(line: str) -> bool:
    stripped = line.strip()
    if not stripped or stripped.startswith(("•", "-", "*")):
        return False
    if len(stripped.split()) > 5:
        return False
    return bool(_HEADER_LINE_RE.match(stripped))


def filter_resume_for_llm(full_text: str) -> str:
    """Only work-experience/project/skills content is ever sent to an LLM —
    name, contact info, links, summary, and education are stripped before
    the resume text reaches build_system_prompt or the LLM-based resume
    classifier. Section boundaries are detected generically (an all-caps,
    <=5-word standalone line, e.g. "WORK EXPERIENCE") rather than matched
    against a fixed set of header strings, so it isn't tied to one resume's
    exact wording — but it does assume ALL-CAPS section headers, which is
    what every resume tested against so far actually uses.

    Content before the first recognized header (name/tagline/contact block)
    is always dropped, whether or not that header is a keep/drop section."""
    sections: list[tuple[str, list[str]]] = []
    current: list[str] | None = None

    for line in full_text.splitlines():
        if _looks_like_section_header(line):
            current = []
            sections.append((line.strip(), current))
            continue
        if current is not None:
            current.append(line)

    kept = [
        "\n".join(content).strip()
        for header, content in sections
        if any(keyword in header.lower() for keyword in _KEEP_SECTION_KEYWORDS)
    ]
    kept = [block for block in kept if block]

    if not kept:
        raise ValueError(
            "no work-experience/project/skills section recognized in this "
            "resume's text — refusing to send its unfiltered content (which "
            "would include name/contact info) to an LLM"
        )
    joined = "\n\n".join(kept)
    return _GITHUB_LINKEDIN_URL_RE.sub("[link removed]", joined)


_BOLD_HEADER_RE = re.compile(r"^\*\*([^*\n]{2,60}?)\*\*:?\s*$")
_ATX_HEADER_RE = re.compile(r"^#{1,6}\s+(.{2,60}?)\s*$")

# Header keywords for sections that are almost always company marketing, not
# role content: substrings, matched case-insensitively, against the header
# text alone (never the body) so a body that happens to mention "benefits"
# in passing doesn't get caught.
_DROP_SECTION_KEYWORDS = (
    "benefit",
    "perk",
    "compensation",
    "salary",
    "pay range",
    "total rewards",
    "why you",
    "why join",
    "why work",
    "our culture",
    "life at",
    "our values",
    "our story",
    "our mission",
    "who we are",
    "what we offer",
    "equal opportunity",
    "equal employment",
    "eeo",
    "diversit",
    "inclusion",
    "accommodation",
    "reasonable adjustment",
    "privacy policy",
    "how to apply",
    "application process",
    "company description",
    "company overview",
    "our commitment",
)
# "About the role/position" describes the job, and "About You" is a common
# stand-in for a qualifications/requirements section (observed on real
# postings, bulleted candidate-fit criteria under exactly that header) —
# both are role content. "About Us"/"About Acme Corp" describes the
# company. Same word, opposite intent — only the header word after "about"
# disambiguates.
_ABOUT_ROLE_EXCEPTIONS = ("role", "position", "job", "opportunity", "you")
# Never drop a section whose body mentions one of these, regardless of its
# header — the scoring rubric's eligibility/location safety net depends on
# this exact kind of statement, and it's cheaper to occasionally keep an
# extra sentence than to silently blind that safety net.
_ELIGIBILITY_OVERRIDE_KEYWORDS = (
    "citizen",
    "clearance",
    "itar",
    "export control",
    "work authorization",
    "authorized to work",
    "visa sponsor",
)


def _looks_like_jd_header(line: str) -> str | None:
    """Return the header text if `line` is a section header, else None.

    Recognizes markdown bold (**Header**), ATX (## Header), and a short
    standalone Title-Case or ALL-CAPS line — the three styles actually seen
    across ATS sources. Plain run-on prose with no line structure at all
    (some ATS sources strip all paragraph breaks upstream) never matches
    anything here, which is intentional: filter_resume_for_llm's caller
    accepts that as "no sections found" and returns the text untouched
    rather than guessing at boundaries in unstructured text."""
    stripped = line.strip()
    if not stripped:
        return None
    if m := _BOLD_HEADER_RE.match(stripped):
        return m.group(1).strip()
    if m := _ATX_HEADER_RE.match(stripped):
        return m.group(1).strip(" *")
    if stripped.startswith(("-", "*", "•", "●", "#")):
        return None
    if stripped.endswith((".", ",", ";")):
        return None
    words = stripped.rstrip(":").split()
    if not words or len(words) > 6:
        return None
    letters_only = "".join(c for c in stripped if c.isalpha())
    if not letters_only:
        return None
    if letters_only.isupper():
        return stripped.rstrip(":")
    # Title-case-ish: every word capitalized except at most one connector
    # (of/the/to/for/we're/&...), e.g. "What We're Looking For".
    capitalized = sum(1 for w in words if w[:1].isupper())
    if capitalized >= max(1, len(words) - 1):
        return stripped.rstrip(":")
    return None


def _is_boilerplate_header(header: str) -> bool:
    lowered = header.lower()
    if lowered.startswith("about") and not any(
        exception in lowered for exception in _ABOUT_ROLE_EXCEPTIONS
    ):
        return True
    return any(keyword in lowered for keyword in _DROP_SECTION_KEYWORDS)


_BULLET_PREFIXES = ("-", "*", "•", "●")
_NUMBERED_LIST_RE = re.compile(r"^\d+[.)]\s")


def _is_bullet_paragraph(text: str) -> bool:
    first_line = text.splitlines()[0].strip()
    return first_line.startswith(_BULLET_PREFIXES) or bool(_NUMBERED_LIST_RE.match(first_line))


def trim_job_description_for_llm(full_text: str) -> str:
    """Strip company-marketing sections (about us, benefits, EEO/diversity
    statements, "why work here") from a job description before it reaches
    the scoring LLM, keeping responsibilities/qualifications content — this
    is pure token-cost reduction, not a privacy control like
    filter_resume_for_llm.

    Header lines are detected individually (a header glued directly to its
    body with no blank line, or directly to the prior section's last line
    with no blank line, is still recognized — both happen on real postings)
    and then regrouped into blank-line-delimited paragraphs *within* each
    header's span, rather than treating "header to next header" as one
    atomic unit. A real posting was observed where a company-mission
    paragraph under "About Acme" was directly followed — same section, no
    new header, just a blank line — by paragraphs that were actually the
    role summary ("As a Senior Engineer on the X team, you will..."):
    dropping the whole "About" span would have silently deleted that role
    content. So under a boilerplate-classified header, only the first
    paragraph and any bulleted/numbered-list paragraphs are dropped (a list
    is always a continuation of the same topic, however many blank lines
    separate its items); a later plain-prose paragraph is a topic shift and
    defaults back to kept, same as unheaded text.

    Content before the first recognized header is always kept in full —
    unlike a resume's contact block, a JD's lead-in sometimes *is* the role
    summary (also observed on real postings), so there's no safe default
    action for it other than to leave it alone.

    Falls back to the untouched original text whenever there's nothing
    reliable to act on: no headers detected at all (common on ATS sources
    that strip paragraph breaks upstream), or the trimmed result collapses
    to near-nothing — the existing character cap downstream is the fallback
    safety net either way, so a no-op here is never a correctness problem,
    only a missed token saving."""
    if not full_text:
        return full_text

    # (header this paragraph falls under, or None for preamble; paragraph text)
    entries: list[tuple[str | None, str]] = []
    buffer: list[str] = []
    current_header: str | None = None
    found_header = False

    def flush() -> None:
        if buffer:
            entries.append((current_header, "\n".join(buffer).strip()))
            buffer.clear()

    for line in full_text.splitlines():
        if line.strip() == "":
            flush()
            continue
        header = _looks_like_jd_header(line)
        if header is not None:
            flush()
            current_header = header
            found_header = True
            continue
        buffer.append(line)
    flush()

    if not found_header:
        return full_text

    kept_blocks: list[str] = []
    last_header: str | None = None
    header_is_boilerplate = False
    header_pending: str | None = None
    paragraphs_under_header = 0

    for header_ctx, text in entries:
        if not text:
            continue

        if header_ctx != last_header:
            last_header = header_ctx
            header_is_boilerplate = header_ctx is not None and _is_boilerplate_header(header_ctx)
            header_pending = header_ctx if (header_ctx is not None and not header_is_boilerplate) else None
            paragraphs_under_header = 0

        if header_ctx is None:
            kept_blocks.append(text)
            continue

        paragraphs_under_header += 1
        is_first = paragraphs_under_header == 1
        is_bullet = _is_bullet_paragraph(text)
        has_override = any(kw in text.lower() for kw in _ELIGIBILITY_OVERRIDE_KEYWORDS)

        if header_is_boilerplate and (is_first or is_bullet) and not has_override:
            continue

        if header_pending is not None:
            kept_blocks.append(header_pending)
            header_pending = None
        kept_blocks.append(text)

    trimmed = "\n\n".join(kept_blocks).strip()

    if len(trimmed) < 100 and len(full_text) > 300:
        return full_text
    return trimmed
