from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path

from pydantic import BaseModel

from coldstart.logging_setup import get_logger
from coldstart.models import RawJob, ResumeId
from coldstart.scoring.base import LLMProvider

logger = get_logger(__name__)

_FDE_RE = re.compile(r"forward deployed|\bFDE\b", re.IGNORECASE)
_AI_RE = re.compile(
    r"\bAI\b|agentic|\bML\b|machine learning|\bLLM\b|applied scientist", re.IGNORECASE
)

# A title that's JUST "Software Engineer" or "SDE"/"SWE" with nothing else is
# too information-poor to trust the keyword default — it escalates to LLM
# routing so the JD content can disambiguate. Anything with additional
# context ("Senior Software Engineer", "Software Engineer, Platform") is
# specific enough to resolve to A directly. DEVELOPMENT_PLAN.md Module 11.
_BARE_GENERIC_TITLES = {"software engineer", "sde", "swe"}


class ResumeEntry(BaseModel):
    file: str
    description: str
    experience_start: date


class ResumeManifest(BaseModel):
    resumes: dict[ResumeId, ResumeEntry]


def load_resume_manifest(path: Path) -> ResumeManifest:
    raw = json.loads(Path(path).read_text())
    return ResumeManifest(resumes=raw)


def route_by_keywords(title: str) -> ResumeId | None:
    title = title or ""
    is_fde = bool(_FDE_RE.search(title))
    is_ai = bool(_AI_RE.search(title))

    if is_fde and is_ai:
        return ResumeId.D
    if is_fde:
        return ResumeId.C
    if is_ai:
        return ResumeId.B

    if title.strip().lower() in _BARE_GENERIC_TITLES:
        return None
    return ResumeId.A


def _parse_resume_id(raw: str) -> ResumeId:
    cleaned = raw.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`")
        if cleaned.startswith("json"):
            cleaned = cleaned[4:]
        cleaned = cleaned.strip()
    data = json.loads(cleaned)
    return ResumeId(data["resume_id"])


_ROUTE_SYSTEM_TEMPLATE = (
    "You are routing a job posting to exactly one of four resume categories "
    "based on its title and description. Respond with strict JSON: "
    '{{"resume_id": "A"|"B"|"C"|"D"}}, nothing else, no markdown fences.\n\n'
    "Categories:\n{manifest_summary}"
)


def route_by_llm(
    title: str, description_head: str, manifest: ResumeManifest, provider: LLMProvider
) -> ResumeId:
    manifest_summary = "\n".join(
        f"{rid.value}: {entry.description}" for rid, entry in manifest.resumes.items()
    )
    system = _ROUTE_SYSTEM_TEMPLATE.format(manifest_summary=manifest_summary)
    user = f"Title: {title}\n\nJob description (excerpt):\n{description_head[:1500]}"

    last_error: Exception | None = None
    for attempt in range(2):
        prompt = (
            user
            if attempt == 0
            else user
            + "\n\nYour previous response did not match the required JSON schema. "
            'Respond with only: {"resume_id": "A"|"B"|"C"|"D"}'
        )
        try:
            response = provider.complete(system, prompt)
            return _parse_resume_id(response.text)
        except Exception as exc:
            last_error = exc
            continue

    logger.warning(
        "LLM routing failed for title %r after retry, falling back to A: %s", title, last_error
    )
    return ResumeId.A


_route_cache: dict[str, tuple[ResumeId, str]] = {}


def clear_route_cache() -> None:
    _route_cache.clear()


def route(job: RawJob, manifest: ResumeManifest, provider: LLMProvider) -> tuple[ResumeId, str]:
    normalized_title = (job.title or "").strip().lower()
    if normalized_title in _route_cache:
        return _route_cache[normalized_title]

    resume_id = route_by_keywords(job.title)
    if resume_id is not None:
        result = (resume_id, "keyword")
    else:
        description_head = (job.description or "")[:1500]
        resume_id = route_by_llm(job.title, description_head, manifest, provider)
        result = (resume_id, "llm")

    _route_cache[normalized_title] = result
    logger.debug("route: %r -> %s (%s)", job.title, result[0].value, result[1])
    return result
