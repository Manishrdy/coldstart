import json

import pytest
from conftest import FakeProvider
from docx import Document

from coldstart.db import connection, init_schema
from coldstart.models import ResumeId
from coldstart.resume_ingest import (
    ExtractionError,
    ResumeClassificationError,
    ResumesNotReady,
    check_resumes_ready,
    classify_slot_from_content,
    classify_slot_from_filename,
    extract_text,
    ingest_resumes,
)


def _make_docx(path, text="Experienced software engineer with Python and SQL skills."):
    doc = Document()
    doc.add_paragraph(text)
    doc.save(str(path))


class _FakePage:
    def __init__(self, text):
        self._text = text

    def extract_text(self):
        return self._text


class _FakePdfReader:
    def __init__(self, path, *, is_encrypted=False, pages=None):
        self.is_encrypted = is_encrypted
        self.pages = pages if pages is not None else [_FakePage("Software engineer resume.")]


@pytest.fixture
def conn(tmp_path):
    with connection(tmp_path / "test.sqlite3") as c:
        init_schema(c)
        yield c


@pytest.fixture
def resumes_dir(tmp_path):
    d = tmp_path / "resumes"
    d.mkdir()
    return d


# --- classify_slot_from_filename --------------------------------------------


@pytest.mark.parametrize(
    "stem,expected",
    [
        ("ManishBotta-SWE", ResumeId.A),
        ("ManishBotta-AIE", ResumeId.B),
        ("ManishBotta-SWE-FDE", ResumeId.C),
        ("ManishBotta-AIE-FDE", ResumeId.D),
        ("general_swe_resume", ResumeId.A),
        ("ai_agentic_resume", ResumeId.B),
        ("fde_general", ResumeId.C),
        ("fde_ai_resume", ResumeId.D),
    ],
)
def test_classify_slot_from_filename_real_and_documented_cases(stem, expected):
    assert classify_slot_from_filename(stem) == expected


def test_classify_slot_from_filename_generic_name_is_none():
    assert classify_slot_from_filename("resume") is None
    assert classify_slot_from_filename("cv_v2") is None


# --- extract_text ------------------------------------------------------------


def test_extract_text_docx_real_file(tmp_path):
    path = tmp_path / "resume.docx"
    _make_docx(path, "Python engineer with 5 years experience.")
    assert "Python engineer" in extract_text(path)


def test_extract_text_pdf_success(tmp_path, monkeypatch):
    path = tmp_path / "resume.pdf"
    path.write_bytes(b"%PDF-1.4 placeholder")
    monkeypatch.setattr(
        "coldstart.resume_ingest.PdfReader",
        lambda p: _FakePdfReader(p, pages=[_FakePage("AI engineer with LangChain experience.")]),
    )
    assert "LangChain" in extract_text(path)


def test_extract_text_pdf_encrypted_raises(tmp_path, monkeypatch):
    path = tmp_path / "resume.pdf"
    path.write_bytes(b"%PDF-1.4 placeholder")
    monkeypatch.setattr(
        "coldstart.resume_ingest.PdfReader", lambda p: _FakePdfReader(p, is_encrypted=True)
    )
    with pytest.raises(ExtractionError, match="password-protected"):
        extract_text(path)


def test_extract_text_pdf_no_text_layer_raises(tmp_path, monkeypatch):
    path = tmp_path / "scanned.pdf"
    path.write_bytes(b"%PDF-1.4 placeholder")
    monkeypatch.setattr(
        "coldstart.resume_ingest.PdfReader", lambda p: _FakePdfReader(p, pages=[_FakePage("")])
    )
    with pytest.raises(ExtractionError, match="no extractable text"):
        extract_text(path)


def test_extract_text_pdf_corrupt_raises(tmp_path, monkeypatch):
    path = tmp_path / "resume.pdf"
    path.write_bytes(b"not a real pdf")

    def _raise(_path):
        raise Exception("malformed PDF structure")

    monkeypatch.setattr("coldstart.resume_ingest.PdfReader", _raise)
    with pytest.raises(ExtractionError, match="could not open"):
        extract_text(path)


def test_extract_text_pdf_page_extraction_failure_raises(tmp_path, monkeypatch):
    path = tmp_path / "resume.pdf"
    path.write_bytes(b"%PDF-1.4 placeholder")

    class _BoomPage:
        def extract_text(self):
            raise RuntimeError("boom")

    monkeypatch.setattr(
        "coldstart.resume_ingest.PdfReader",
        lambda p: _FakePdfReader(p, pages=[_BoomPage()]),
    )
    with pytest.raises(ExtractionError, match="could not extract text"):
        extract_text(path)


def test_extract_text_docx_open_failure_raises(tmp_path, monkeypatch):
    path = tmp_path / "resume.docx"
    path.write_bytes(b"not a real docx")

    def _raise(_path):
        raise Exception("bad zip file")

    monkeypatch.setattr("coldstart.resume_ingest.Document", _raise)
    with pytest.raises(ExtractionError, match="could not open"):
        extract_text(path)


def test_extract_text_unsupported_type_raises(tmp_path):
    path = tmp_path / "resume.txt"
    path.write_text("hello")
    with pytest.raises(ExtractionError, match="unsupported file type"):
        extract_text(path)


# --- classify_slot_from_content ----------------------------------------------


def test_classify_slot_from_content_success():
    provider = FakeProvider(['{"resume_id": "B"}'])
    assert classify_slot_from_content("resume text", [provider]) == ResumeId.B
    assert provider.calls == 1


def test_classify_slot_from_content_retries_then_succeeds():
    provider = FakeProvider(["not json", '{"resume_id": "C"}'])
    assert classify_slot_from_content("resume text", [provider]) == ResumeId.C
    assert provider.calls == 2


def test_classify_slot_from_content_parses_markdown_fenced_json():
    provider = FakeProvider(['```json\n{"resume_id": "D"}\n```'])
    assert classify_slot_from_content("resume text", [provider]) == ResumeId.D


def test_classify_slot_from_content_exhausts_all_providers_raises():
    p1 = FakeProvider(["garbage", "garbage"])
    p2 = FakeProvider(["still garbage", "still garbage"])
    with pytest.raises(ResumeClassificationError):
        classify_slot_from_content("resume text", [p1, p2])


# --- check_resumes_ready / ingest_resumes orchestration ----------------------


def test_check_resumes_ready_false_when_empty(resumes_dir):
    assert check_resumes_ready(resumes_dir, resumes_dir / "manifest.json") is False


def test_ingest_resumes_raises_when_empty(resumes_dir, conn):
    with pytest.raises(ResumesNotReady) as exc_info:
        ingest_resumes(resumes_dir, resumes_dir / "manifest.json", [], conn)
    message = str(exc_info.value)
    for slot in ("A", "B", "C", "D"):
        assert slot in message


def test_ingest_resumes_full_set_resolves_via_filename_and_archives(resumes_dir, conn):
    _make_docx(resumes_dir / "general_swe.docx", "General SWE resume text.")
    _make_docx(resumes_dir / "ai_agentic.docx", "AI agentic resume text.")
    _make_docx(resumes_dir / "fde_general.docx", "FDE general resume text.")
    _make_docx(resumes_dir / "fde_ai.docx", "FDE AI resume text.")

    manifest_path = resumes_dir / "manifest.json"
    resolved = ingest_resumes(resumes_dir, manifest_path, [], conn)

    assert set(resolved) == {ResumeId.A, ResumeId.B, ResumeId.C, ResumeId.D}
    assert resolved[ResumeId.A].is_fde is False
    assert resolved[ResumeId.D].is_fde is True
    assert resolved[ResumeId.D].is_ai is True

    for filename in (
        "general_swe.docx",
        "ai_agentic.docx",
        "fde_general.docx",
        "fde_ai.docx",
    ):
        assert not (resumes_dir / filename).exists()
        assert (resumes_dir / "originals" / filename).exists()

    for slot_file in (
        "resume_a_swe.json",
        "resume_b_ai.json",
        "resume_c_fde_swe.json",
        "resume_d_fde_ai.json",
    ):
        assert (resumes_dir / slot_file).exists()

    manifest = json.loads(manifest_path.read_text())
    assert manifest["A"]["file"] == "resume_a_swe.json"
    assert manifest["A"]["description"]


def test_ingest_resumes_mixed_pdf_and_docx(resumes_dir, conn, monkeypatch):
    _make_docx(resumes_dir / "general_swe.docx", "General SWE.")
    _make_docx(resumes_dir / "fde_general.docx", "FDE general.")
    (resumes_dir / "ai_agentic.pdf").write_bytes(b"%PDF placeholder")
    (resumes_dir / "fde_ai.pdf").write_bytes(b"%PDF placeholder 2")

    monkeypatch.setattr(
        "coldstart.resume_ingest.PdfReader",
        lambda p: _FakePdfReader(p, pages=[_FakePage("AI resume content.")]),
    )

    resolved = ingest_resumes(resumes_dir, resumes_dir / "manifest.json", [], conn)
    assert set(resolved) == {ResumeId.A, ResumeId.B, ResumeId.C, ResumeId.D}
    assert (resumes_dir / "originals" / "ai_agentic.pdf").exists()
    assert (resumes_dir / "originals" / "general_swe.docx").exists()


def test_ingest_resumes_ambiguous_filename_uses_llm_fallback(resumes_dir, conn):
    _make_docx(resumes_dir / "resume.docx", "General SWE, Python and Go.")
    _make_docx(resumes_dir / "ai_agentic.docx", "AI resume text.")
    _make_docx(resumes_dir / "fde_general.docx", "FDE general.")
    _make_docx(resumes_dir / "fde_ai.docx", "FDE AI.")

    provider = FakeProvider(['{"resume_id": "A"}'])
    resolved = ingest_resumes(resumes_dir, resumes_dir / "manifest.json", [provider], conn)
    assert provider.calls == 1
    assert resolved[ResumeId.A].source_filename == "resume.docx"


def test_ingest_resumes_llm_fallback_exhausted_raises(resumes_dir, conn):
    _make_docx(resumes_dir / "resume.docx", "Generic resume text.")
    _make_docx(resumes_dir / "ai_agentic.docx", "AI resume text.")
    _make_docx(resumes_dir / "fde_general.docx", "FDE general.")
    _make_docx(resumes_dir / "fde_ai.docx", "FDE AI.")

    provider = FakeProvider(["garbage", "garbage"])
    with pytest.raises(ResumesNotReady, match="resume.docx"):
        ingest_resumes(resumes_dir, resumes_dir / "manifest.json", [provider], conn)


def test_ingest_resumes_slot_collision_raises(resumes_dir, conn):
    _make_docx(resumes_dir / "ai_one.docx", "AI resume one.")
    _make_docx(resumes_dir / "ai_two.docx", "AI resume two.")
    with pytest.raises(ResumesNotReady, match="ai_one.docx"):
        ingest_resumes(resumes_dir, resumes_dir / "manifest.json", [], conn)


def test_ingest_resumes_partial_coverage_raises(resumes_dir, conn):
    _make_docx(resumes_dir / "general_swe.docx", "General SWE.")
    _make_docx(resumes_dir / "ai_agentic.docx", "AI resume.")
    _make_docx(resumes_dir / "fde_general.docx", "FDE general.")
    with pytest.raises(ResumesNotReady, match="D"):
        ingest_resumes(resumes_dir, resumes_dir / "manifest.json", [], conn)


def test_ingest_resumes_corrupt_pdf_raises_and_logs_error(resumes_dir, conn, monkeypatch):
    (resumes_dir / "general_swe.pdf").write_bytes(b"not a real pdf")
    _make_docx(resumes_dir / "ai_agentic.docx", "AI resume.")
    _make_docx(resumes_dir / "fde_general.docx", "FDE general.")
    _make_docx(resumes_dir / "fde_ai.docx", "FDE AI.")

    def _raise(_path):
        raise Exception("malformed PDF structure")

    monkeypatch.setattr("coldstart.resume_ingest.PdfReader", _raise)
    with pytest.raises(ResumesNotReady, match="general_swe.pdf"):
        ingest_resumes(resumes_dir, resumes_dir / "manifest.json", [], conn)

    row = conn.execute("SELECT stage, job_ref FROM errors").fetchone()
    assert row["stage"] == "resume_ingest"
    assert row["job_ref"] == "general_swe.pdf"


def test_scanned_pdf_blocks_ingestion_not_silently_empty(resumes_dir, conn, monkeypatch):
    (resumes_dir / "general_swe.pdf").write_bytes(b"%PDF placeholder")
    _make_docx(resumes_dir / "ai_agentic.docx", "AI resume.")
    _make_docx(resumes_dir / "fde_general.docx", "FDE general.")
    _make_docx(resumes_dir / "fde_ai.docx", "FDE AI.")

    monkeypatch.setattr(
        "coldstart.resume_ingest.PdfReader", lambda p: _FakePdfReader(p, pages=[_FakePage("")])
    )
    with pytest.raises(ResumesNotReady, match="no extractable text"):
        ingest_resumes(resumes_dir, resumes_dir / "manifest.json", [], conn)
    assert not (resumes_dir / "resume_a_swe.json").exists()


def test_check_resumes_ready_true_after_ingest_and_reingest_is_noop(resumes_dir, conn, monkeypatch):
    _make_docx(resumes_dir / "general_swe.docx", "General SWE.")
    _make_docx(resumes_dir / "ai_agentic.docx", "AI resume.")
    _make_docx(resumes_dir / "fde_general.docx", "FDE general.")
    _make_docx(resumes_dir / "fde_ai.docx", "FDE AI.")
    manifest_path = resumes_dir / "manifest.json"

    ingest_resumes(resumes_dir, manifest_path, [], conn)
    assert check_resumes_ready(resumes_dir, manifest_path) is True

    calls = {"n": 0}
    from coldstart import resume_ingest as ri

    original_extract = ri.extract_text

    def _tracking_extract(path):
        calls["n"] += 1
        return original_extract(path)

    monkeypatch.setattr("coldstart.resume_ingest.extract_text", _tracking_extract)
    resolved = ingest_resumes(resumes_dir, manifest_path, [], conn)
    assert calls["n"] == 0
    assert set(resolved) == {ResumeId.A, ResumeId.B, ResumeId.C, ResumeId.D}


def test_check_resumes_ready_false_when_slot_json_is_corrupt(resumes_dir, conn):
    _make_docx(resumes_dir / "general_swe.docx", "General SWE.")
    _make_docx(resumes_dir / "ai_agentic.docx", "AI resume.")
    _make_docx(resumes_dir / "fde_general.docx", "FDE general.")
    _make_docx(resumes_dir / "fde_ai.docx", "FDE AI.")
    manifest_path = resumes_dir / "manifest.json"
    ingest_resumes(resumes_dir, manifest_path, [], conn)

    (resumes_dir / "resume_a_swe.json").write_text("{not valid json")
    assert check_resumes_ready(resumes_dir, manifest_path) is False


def test_check_resumes_ready_false_with_only_three_slots(resumes_dir, conn):
    _make_docx(resumes_dir / "general_swe.docx", "General SWE.")
    _make_docx(resumes_dir / "ai_agentic.docx", "AI resume.")
    _make_docx(resumes_dir / "fde_general.docx", "FDE general.")
    _make_docx(resumes_dir / "fde_ai.docx", "FDE AI.")
    manifest_path = resumes_dir / "manifest.json"
    ingest_resumes(resumes_dir, manifest_path, [], conn)

    (resumes_dir / "resume_d_fde_ai.json").unlink()
    assert check_resumes_ready(resumes_dir, manifest_path) is False


def test_ingest_resumes_source_file_changed_reextracts(resumes_dir, conn):
    path = resumes_dir / "general_swe.docx"
    _make_docx(path, "Original resume text version one.")
    _make_docx(resumes_dir / "ai_agentic.docx", "AI resume.")
    _make_docx(resumes_dir / "fde_general.docx", "FDE general.")
    _make_docx(resumes_dir / "fde_ai.docx", "FDE AI.")
    manifest_path = resumes_dir / "manifest.json"

    ingest_resumes(resumes_dir, manifest_path, [], conn)
    assert (resumes_dir / "originals" / "general_swe.docx").exists()

    _make_docx(path, "Updated resume text version two, now with Kubernetes.")
    resolved = ingest_resumes(resumes_dir, manifest_path, [], conn)
    assert "Kubernetes" in resolved[ResumeId.A].full_text
