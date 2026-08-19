import json
from datetime import date
from pathlib import Path

import pytest
from freezegun import freeze_time

from coldstart.settings import ConfigError, load_settings, years_of_experience

REQUIRED_ENV = {
    "EXPERIENCE_START_DATE": "2020-01-01",
    "SMTP_USER": "user@example.com",
    "SMTP_APP_PASSWORD": "app-password",
    "DIGEST_RECIPIENT": "user@example.com",
    "DEEPSEEK_API_KEY": "sk-deepseek",
    "KIMI_API_KEY": "sk-kimi",
}


@pytest.fixture
def valid_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    return tmp_path


def test_valid_env_loads(valid_env):
    settings = load_settings()
    assert settings.experience_start_date == date(2020, 1, 1)
    assert settings.smtp_user == "user@example.com"
    assert settings.provider_fallback_order == ["deepseek", "kimi"]
    assert settings.llm_request_timeout_seconds == 120.0


def test_llm_request_timeout_seconds_overridable(monkeypatch, valid_env):
    monkeypatch.setenv("LLM_REQUEST_TIMEOUT_SECONDS", "30")
    settings = load_settings()
    assert settings.llm_request_timeout_seconds == 30.0


def test_missing_api_key_for_fallback_provider_raises(monkeypatch, valid_env):
    monkeypatch.delenv("KIMI_API_KEY")
    with pytest.raises(ConfigError) as exc_info:
        load_settings()
    assert any("kimi_api_key" in p for p in exc_info.value.problems)


def test_unknown_provider_raises(monkeypatch, valid_env):
    monkeypatch.setenv("PROVIDER_FALLBACK_ORDER", "deepseek,notaprovider")
    with pytest.raises(ConfigError) as exc_info:
        load_settings()
    assert any("notaprovider" in p for p in exc_info.value.problems)


def test_dev_mode_skips_api_key_requirement(monkeypatch, valid_env):
    monkeypatch.delenv("KIMI_API_KEY")
    monkeypatch.delenv("DEEPSEEK_API_KEY")
    monkeypatch.setenv("LLM_MODE", "dev")
    monkeypatch.setenv("OLLAMA_MODEL", "qwen2.5:3b")
    settings = load_settings()
    assert settings.llm_mode == "dev"


def test_dev_mode_requires_ollama_model(monkeypatch, valid_env):
    monkeypatch.setenv("LLM_MODE", "dev")
    with pytest.raises(ConfigError) as exc_info:
        load_settings()
    assert any("ollama_model" in p for p in exc_info.value.problems)


def test_consider_gte_strong_raises(monkeypatch, valid_env):
    monkeypatch.setenv("SCORE_THRESHOLD_CONSIDER", "80")
    monkeypatch.setenv("SCORE_THRESHOLD_STRONG", "70")
    with pytest.raises(ConfigError) as exc_info:
        load_settings()
    assert any("score_threshold_consider" in p for p in exc_info.value.problems)


def test_bad_digest_time_raises(monkeypatch, valid_env):
    monkeypatch.setenv("DIGEST_TIME_PDT", "8am")
    with pytest.raises(ConfigError) as exc_info:
        load_settings()
    assert any("digest_time_pdt" in p for p in exc_info.value.problems)


def test_experience_start_date_in_future_raises(monkeypatch, valid_env):
    monkeypatch.setenv("EXPERIENCE_START_DATE", "2999-01-01")
    with pytest.raises(ConfigError) as exc_info:
        load_settings()
    assert any("experience_start_date" in p for p in exc_info.value.problems)


def test_experience_start_date_too_old_raises(monkeypatch, valid_env):
    monkeypatch.setenv("EXPERIENCE_START_DATE", "2010-01-01")
    with pytest.raises(ConfigError) as exc_info:
        load_settings()
    assert any("experience_start_date" in p for p in exc_info.value.problems)


def test_missing_resume_manifest_is_not_an_error(valid_env):
    settings = load_settings()
    assert not settings.resume_manifest.exists()


def test_resume_manifest_referencing_missing_file_raises(valid_env):
    manifest_dir = valid_env / "config" / "resumes"
    manifest_dir.mkdir(parents=True)
    manifest_path = manifest_dir / "manifest.json"
    manifest_path.write_text(json.dumps({"A": {"file": "resume_a_swe.json", "description": "x"}}))
    with pytest.raises(ConfigError) as exc_info:
        load_settings()
    assert any("resume file for 'A'" in p for p in exc_info.value.problems)


def test_resume_manifest_with_valid_files_loads(valid_env):
    manifest_dir = valid_env / "config" / "resumes"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "resume_a_swe.json").write_text("{}")
    manifest_path = manifest_dir / "manifest.json"
    manifest_path.write_text(json.dumps({"A": {"file": "resume_a_swe.json", "description": "x"}}))
    settings = load_settings()
    assert settings.resume_manifest == Path("config/resumes/manifest.json")


def test_resume_manifest_malformed_json_raises(valid_env):
    manifest_dir = valid_env / "config" / "resumes"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "manifest.json").write_text("{not valid json")
    with pytest.raises(ConfigError) as exc_info:
        load_settings()
    assert any("not valid JSON" in p for p in exc_info.value.problems)


def test_resume_manifest_entry_missing_file_field_raises(valid_env):
    manifest_dir = valid_env / "config" / "resumes"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "manifest.json").write_text(json.dumps({"A": {"description": "x"}}))
    with pytest.raises(ConfigError) as exc_info:
        load_settings()
    assert any("no 'file' field" in p for p in exc_info.value.problems)


def test_resume_manifest_empty_file_raises(valid_env):
    manifest_dir = valid_env / "config" / "resumes"
    manifest_dir.mkdir(parents=True)
    (manifest_dir / "resume_a_swe.json").write_text("")
    manifest_dir_manifest = manifest_dir / "manifest.json"
    manifest_dir_manifest.write_text(
        json.dumps({"A": {"file": "resume_a_swe.json", "description": "x"}})
    )
    with pytest.raises(ConfigError) as exc_info:
        load_settings()
    assert any("is empty" in p for p in exc_info.value.problems)


def test_uncreatable_dir_raises(monkeypatch, valid_env):
    from pathlib import Path as PathClass

    original_mkdir = PathClass.mkdir

    def fake_mkdir(self, *args, **kwargs):
        if self.name == "data":
            raise OSError("permission denied")
        return original_mkdir(self, *args, **kwargs)

    monkeypatch.setattr(PathClass, "mkdir", fake_mkdir)
    with pytest.raises(ConfigError) as exc_info:
        load_settings()
    assert any("data_dir" in p for p in exc_info.value.problems)


def test_multiple_simultaneous_failures_all_reported(monkeypatch, valid_env):
    monkeypatch.setenv("SCORE_THRESHOLD_CONSIDER", "80")
    monkeypatch.setenv("DIGEST_TIME_PDT", "not-a-time")
    monkeypatch.delenv("KIMI_API_KEY")
    with pytest.raises(ConfigError) as exc_info:
        load_settings()
    problems = exc_info.value.problems
    assert len(problems) >= 3
    assert any("score_threshold_consider" in p for p in problems)
    assert any("digest_time_pdt" in p for p in problems)
    assert any("kimi_api_key" in p for p in problems)


def test_missing_required_field_raises_config_error(monkeypatch, valid_env):
    monkeypatch.delenv("SMTP_USER")
    with pytest.raises(ConfigError):
        load_settings()


@freeze_time("2026-08-18")
def test_years_of_experience_boundary():
    assert years_of_experience(date(2025, 2, 1)) == pytest.approx(1.54, abs=0.01)


def test_years_of_experience_explicit_today():
    yoe = years_of_experience(date(2020, 1, 1), today=date(2021, 1, 1))
    assert yoe == pytest.approx(1.0, abs=0.01)
