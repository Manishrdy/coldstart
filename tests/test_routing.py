import json

import pytest
from conftest import FakeProvider

from coldstart.models import RawJob, ResumeId
from coldstart.routing import (
    ResumeEntry,
    ResumeManifest,
    clear_route_cache,
    load_resume_manifest,
    route,
    route_by_keywords,
    route_by_llm,
)


@pytest.fixture(autouse=True)
def _reset_cache():
    clear_route_cache()
    yield
    clear_route_cache()


def _manifest() -> ResumeManifest:
    return ResumeManifest(
        resumes={
            ResumeId.A: ResumeEntry(file="resume_a_swe.json", description="General SWE"),
            ResumeId.B: ResumeEntry(
                file="resume_b_ai.json", description="AI/Agentic Engineer"
            ),
            ResumeId.C: ResumeEntry(file="resume_c_fde_swe.json", description="FDE general"),
            ResumeId.D: ResumeEntry(file="resume_d_fde_ai.json", description="FDE AI"),
        }
    )


def _job(**overrides) -> RawJob:
    kwargs = dict(
        global_id="g:1",
        company="Acme",
        title="Software Engineer",
        url="https://x",
        ats_type="greenhouse",
        description="Great team.",
    )
    kwargs.update(overrides)
    return RawJob(**kwargs)


# --- route_by_keywords -------------------------------------------------------


@pytest.mark.parametrize(
    "title,expected",
    [
        ("Senior Software Engineer", ResumeId.A),
        ("Staff Software Engineer", ResumeId.A),
        ("Software Engineer, Platform", ResumeId.A),
        ("AI Engineer", ResumeId.B),
        ("Agentic Engineer", ResumeId.B),
        ("ML Engineer", ResumeId.B),
        ("Machine Learning Engineer", ResumeId.B),
        ("LLM Engineer", ResumeId.B),
        ("Applied Scientist", ResumeId.B),
        ("Forward Deployed Engineer", ResumeId.C),
        ("FDE", ResumeId.C),
        ("Forward Deployed Engineer, AI", ResumeId.D),
        ("FDE, Agentic", ResumeId.D),
    ],
)
def test_route_by_keywords_2x2_matrix(title, expected):
    assert route_by_keywords(title) == expected


@pytest.mark.parametrize("title", ["Software Engineer", "SDE", "SWE", "  software engineer  "])
def test_route_by_keywords_bare_generic_titles_escalate(title):
    assert route_by_keywords(title) is None


def test_route_by_keywords_empty_title_is_not_bare_generic():
    # Not "software engineer"/"sde"/"swe" -> resolves to A, doesn't escalate
    assert route_by_keywords("") == ResumeId.A


# --- route_by_llm ------------------------------------------------------------


def test_route_by_llm_success():
    provider = FakeProvider(['{"resume_id": "B"}'])
    result = route_by_llm("Software Engineer", "AI-heavy JD body", _manifest(), provider)
    assert result == ResumeId.B
    assert provider.calls == 1


def test_route_by_llm_parses_markdown_fenced_json():
    provider = FakeProvider(['```json\n{"resume_id": "C"}\n```'])
    result = route_by_llm("Engineer", "some JD", _manifest(), provider)
    assert result == ResumeId.C


def test_route_by_llm_retries_then_succeeds():
    provider = FakeProvider(["not json", '{"resume_id": "D"}'])
    result = route_by_llm("Engineer", "some JD", _manifest(), provider)
    assert result == ResumeId.D
    assert provider.calls == 2


def test_route_by_llm_garbage_falls_back_to_a_with_warning(caplog):
    import logging

    provider = FakeProvider(["garbage", "still garbage"])
    with caplog.at_level(logging.WARNING, logger="coldstart.routing"):
        result = route_by_llm("Engineer", "some JD", _manifest(), provider)
    assert result == ResumeId.A
    assert any("falling back to A" in r.getMessage() for r in caplog.records)


def test_route_by_llm_never_raises_on_provider_exception():
    provider = FakeProvider([RuntimeError("boom"), RuntimeError("boom again")])
    result = route_by_llm("Engineer", "some JD", _manifest(), provider)
    assert result == ResumeId.A


# --- route (orchestration + cache) -------------------------------------------


def test_route_keyword_path_does_not_call_llm():
    provider = FakeProvider([])
    resume_id, method = route(_job(title="AI Engineer"), _manifest(), provider)
    assert resume_id == ResumeId.B
    assert method == "keyword"
    assert provider.calls == 0


def test_route_generic_title_with_ai_heavy_jd_escalates_to_llm():
    provider = FakeProvider(['{"resume_id": "B"}'])
    job = _job(title="Software Engineer", description="Build agentic LLM pipelines all day.")
    resume_id, method = route(job, _manifest(), provider)
    assert resume_id == ResumeId.B
    assert method == "llm"
    assert provider.calls == 1


def test_route_title_cache_hits_llm_once_for_two_identical_titles():
    provider = FakeProvider(['{"resume_id": "A"}'])
    job1 = _job(global_id="g:1", title="Software Engineer", description="x")
    job2 = _job(global_id="g:2", title="Software Engineer", description="x")

    route(job1, _manifest(), provider)
    route(job2, _manifest(), provider)

    assert provider.calls == 1


def test_route_title_cache_is_case_and_whitespace_insensitive():
    provider = FakeProvider(['{"resume_id": "A"}'])
    route(_job(title="Software Engineer"), _manifest(), provider)
    route(_job(title="  software engineer  "), _manifest(), provider)
    assert provider.calls == 1


def test_route_caches_keyword_results_too():
    provider = FakeProvider([])
    route(_job(title="AI Engineer"), _manifest(), provider)
    route(_job(title="AI Engineer"), _manifest(), provider)
    assert provider.calls == 0


def test_clear_route_cache_resets_state():
    provider = FakeProvider(['{"resume_id": "A"}', '{"resume_id": "A"}'])
    route(_job(title="Software Engineer"), _manifest(), provider)
    clear_route_cache()
    route(_job(title="Software Engineer"), _manifest(), provider)
    assert provider.calls == 2


# --- load_resume_manifest -----------------------------------------------------


def test_load_resume_manifest(tmp_path):
    manifest_path = tmp_path / "manifest.json"
    manifest_path.write_text(
        json.dumps(
            {
                "A": {"file": "resume_a_swe.json", "description": "General SWE"},
                "B": {"file": "resume_b_ai.json", "description": "AI Engineer"},
                "C": {"file": "resume_c_fde_swe.json", "description": "FDE general"},
                "D": {"file": "resume_d_fde_ai.json", "description": "FDE AI"},
            }
        )
    )
    manifest = load_resume_manifest(manifest_path)
    assert manifest.resumes[ResumeId.A].description == "General SWE"
    assert manifest.resumes[ResumeId.B].file == "resume_b_ai.json"
