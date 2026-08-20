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
