from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from docx import Document
from pydantic import BaseModel, ValidationError
from pypdf import PdfReader

from coldstart.errors import log_error
from coldstart.logging_setup import get_logger
from coldstart.models import ResumeId
from coldstart.scoring.base import LLMProvider
from coldstart.text_utils import filter_resume_for_llm, strip_markdown_json_fences

logger = get_logger(__name__)

_SLOT_FILENAME = {
    ResumeId.A: "resume_a_swe.json",
    ResumeId.B: "resume_b_ai.json",
    ResumeId.C: "resume_c_fde_swe.json",
    ResumeId.D: "resume_d_fde_ai.json",
}

_SLOT_DESCRIPTIONS = {
    ResumeId.A: "General Software Engineer / SDE",
    ResumeId.B: "AI / Agentic Engineer",
    ResumeId.C: "Forward Deployed Engineer, general SWE flavor",
    ResumeId.D: "Forward Deployed Engineer, AI-specific flavor",
}

# Filename markers (scope.md §5.1). "general" markers resolve confidently to A;
# a stem with none of the three categories is genuinely ambiguous (e.g. "resume.pdf")
# and escalates to content classification rather than silently guessing A.
_FDE_MARKERS = ("fde",)
_AI_MARKERS = ("ai", "agentic", "ml")
_GENERAL_MARKERS = ("swe", "general", "standard", "software")

_CLASSIFY_SYSTEM_PROMPT = (
    "You are classifying a resume into exactly one of four categories based on its "
    "content. Respond with strict JSON: {\"resume_id\": \"A\"|\"B\"|\"C\"|\"D\"}, nothing "
    "else, no markdown fences.\n"
    "A = general Software Engineer / SDE, not forward-deployed, not AI-specialized.\n"
    "B = AI / Agentic Engineer, not forward-deployed.\n"
    "C = Forward Deployed Engineer, general software engineering flavor.\n"
    "D = Forward Deployed Engineer, AI-specific flavor.\n"
)


class NormalizedResume(BaseModel):
    resume_id: ResumeId
    source_filename: str
    source_sha256: str
    extracted_at: datetime
    is_fde: bool
    is_ai: bool
    description: str
    full_text: str


class ResumesNotReady(Exception):
    """Raised when the 4-slot resume set can't be resolved. Message is human-readable."""


class ExtractionError(Exception):
    """Text couldn't be pulled from a PDF/DOCX (corrupt, encrypted, or no text layer)."""


class ResumeClassificationError(Exception):
    """LLM fallback classification failed after exhausting retries."""


def extract_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        raw = _extract_pdf_text(path)
    elif suffix == ".docx":
        raw = _extract_docx_text(path)
    else:
        raise ExtractionError(f"unsupported file type: {path.name}")

    text = _clean_text(raw)
    if not text:
        raise ExtractionError(
            f"{path.name} produced no extractable text — possibly a scanned/"
            "image-only PDF (OCR is not supported)"
        )
    return text


def _extract_pdf_text(path: Path) -> str:
    try:
        reader = PdfReader(str(path))
    except Exception as exc:
        raise ExtractionError(f"could not open {path.name}: {exc}") from exc

    if reader.is_encrypted:
        raise ExtractionError(f"{path.name} is password-protected")

    try:
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as exc:
        raise ExtractionError(f"could not extract text from {path.name}: {exc}") from exc


def _extract_docx_text(path: Path) -> str:
    try:
        document = Document(str(path))
    except Exception as exc:
        raise ExtractionError(f"could not open {path.name}: {exc}") from exc
    return "\n".join(p.text for p in document.paragraphs)


def _clean_text(raw: str) -> str:
    text = re.sub(r"-\n(?=\w)", "", raw)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def classify_slot_from_filename(stem: str) -> ResumeId | None:
    lowered = stem.lower()
    has_fde = any(marker in lowered for marker in _FDE_MARKERS)
    has_ai = any(marker in lowered for marker in _AI_MARKERS)
    has_general = any(marker in lowered for marker in _GENERAL_MARKERS)

    if not (has_fde or has_ai or has_general):
        return None
    if has_fde and has_ai:
        return ResumeId.D
    if has_fde:
        return ResumeId.C
    if has_ai:
        return ResumeId.B
    return ResumeId.A


def classify_slot_from_content(text: str, provider: LLMProvider) -> ResumeId:
    # Same PII policy as scoring (rubric.build_system_prompt) — this is an
    # LLM call too, so name/contact/summary/education never reach it either.
    excerpt = filter_resume_for_llm(text)[:4000]
    last_error: Exception | None = None
    for attempt in range(2):
        prompt = (
            excerpt
            if attempt == 0
            else excerpt
            + "\n\nYour previous response did not match the required JSON schema. "
            'Respond with only: {"resume_id": "A"|"B"|"C"|"D"}'
        )
        try:
            response = provider.complete(_CLASSIFY_SYSTEM_PROMPT, prompt)
            resume_id = _parse_classification(response.text)
            logger.warning("resume slot resolved via LLM fallback: %s", resume_id.value)
            return resume_id
        except Exception as exc:
            last_error = exc
            continue
    raise ResumeClassificationError(
        f"could not classify resume via LLM after exhausting retries: {last_error}"
    )


def _parse_classification(raw: str) -> ResumeId:
    cleaned = strip_markdown_json_fences(raw)
    data = json.loads(cleaned)
    return ResumeId(data["resume_id"])


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 16), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _scan_sources(resumes_dir: Path) -> list[Path]:
    return sorted(
        p
        for p in resumes_dir.iterdir()
        if p.is_file() and p.suffix.lower() in (".pdf", ".docx")
    )


def load_existing_slots(resumes_dir: Path) -> dict[ResumeId, NormalizedResume]:
    existing: dict[ResumeId, NormalizedResume] = {}
    for slot, filename in _SLOT_FILENAME.items():
        path = resumes_dir / filename
        if path.exists() and path.stat().st_size > 0:
            try:
                existing[slot] = NormalizedResume.model_validate_json(path.read_text())
            except (ValidationError, ValueError):
                continue
    return existing


def check_resumes_ready(resumes_dir: Path, manifest_path: Path) -> bool:
    resumes_dir = Path(resumes_dir)
    if not resumes_dir.exists() or not manifest_path.exists():
        return False

    existing = load_existing_slots(resumes_dir)
    if len(existing) != 4:
        return False

    recorded = {r.source_filename: r.source_sha256 for r in existing.values()}
    for source in _scan_sources(resumes_dir):
        if recorded.get(source.name) != _sha256_file(source):
            return False
    return True


def _raise_not_ready(
    conn: sqlite3.Connection,
    message: str,
    exc: Exception | None = None,
    job_ref: str | None = None,
) -> None:
    logger.critical(message)
    log_error(
        conn,
        stage="resume_ingest",
        exc=exc,
        message=None if exc else message,
        source_file=__name__,
        function_name="ingest_resumes",
        job_ref=job_ref,
    )
    raise ResumesNotReady(message) from exc


def ingest_resumes(
    resumes_dir: Path,
    manifest_path: Path,
    provider: LLMProvider,
    conn: sqlite3.Connection,
) -> dict[ResumeId, NormalizedResume]:
    resumes_dir = Path(resumes_dir)
    resumes_dir.mkdir(parents=True, exist_ok=True)
    originals_dir = resumes_dir / "originals"

    existing = load_existing_slots(resumes_dir)
    sources = _scan_sources(resumes_dir)

    if not sources and len(existing) < 4:
        missing = [s.value for s in ResumeId if s not in existing]
        _raise_not_ready(
            conn,
            "No resumes found in config/resumes/. Add 4 resumes (PDF or DOCX) covering: "
            "general SWE, AI/Agentic Engineer, FDE (general), FDE (AI). "
            f"Missing slot(s): {', '.join(missing)}.",
        )

    recorded = {r.source_filename: r for r in existing.values()}
    to_process = [
        s
        for s in sources
        if s.name not in recorded or recorded[s.name].source_sha256 != _sha256_file(s)
    ]

    classified: list[tuple[Path, ResumeId, str, str]] = []
    for source in to_process:
        try:
            text = extract_text(source)
        except ExtractionError as exc:
            _raise_not_ready(
                conn, f"failed to extract text from {source.name}: {exc}", exc, source.name
            )

        slot = classify_slot_from_filename(source.stem)
        method = "filename"
        if slot is None:
            try:
                slot = classify_slot_from_content(text, provider)
                method = "llm"
            except ResumeClassificationError as exc:
                _raise_not_ready(
                    conn,
                    f"could not determine resume slot for {source.name} from its filename "
                    "or content — rename it to include a marker (fde, ai, agentic, ml, "
                    "swe, general, standard, software)",
                    exc,
                    source.name,
                )
        classified.append((source, slot, method, text))

    by_slot: dict[ResumeId, list[Path]] = {}
    for source, slot, _method, _text in classified:
        by_slot.setdefault(slot, []).append(source)
    collisions = {slot: paths for slot, paths in by_slot.items() if len(paths) > 1}
    if collisions:
        detail = "; ".join(
            f"{slot.value}: {', '.join(p.name for p in paths)}"
            for slot, paths in collisions.items()
        )
        _raise_not_ready(conn, f"multiple resumes resolved to the same slot — {detail}")

    resolved = dict(existing)
    for source, slot, method, text in classified:
        logger.info("resume %s -> slot %s (via %s)", source.name, slot.value, method)
        resolved[slot] = NormalizedResume(
            resume_id=slot,
            source_filename=source.name,
            source_sha256=_sha256_file(source),
            extracted_at=datetime.now(UTC),
            is_fde=slot in (ResumeId.C, ResumeId.D),
            is_ai=slot in (ResumeId.B, ResumeId.D),
            description=_SLOT_DESCRIPTIONS[slot],
            full_text=text,
        )

    missing = [s.value for s in ResumeId if s not in resolved]
    if missing:
        _raise_not_ready(conn, f"missing resume slot(s): {', '.join(missing)}")

    for slot, record in resolved.items():
        (resumes_dir / _SLOT_FILENAME[slot]).write_text(record.model_dump_json(indent=2))

    if classified:
        originals_dir.mkdir(exist_ok=True)
        for source, _slot, _method, _text in classified:
            source.replace(originals_dir / source.name)

    _write_manifest(manifest_path, resolved)
    logger.info("%d/4 resume slots resolved", len(resolved))
    return resolved


def _write_manifest(manifest_path: Path, resolved: dict[ResumeId, NormalizedResume]) -> None:
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest = {
        slot.value: {
            "file": _SLOT_FILENAME[slot],
            "description": record.description,
        }
        for slot, record in resolved.items()
    }
    manifest_path.write_text(json.dumps(manifest, indent=2, sort_keys=True))
