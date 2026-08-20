import json
from pathlib import Path

import pytest

from coldstart.settings import ConfigError, load_settings

REQUIRED_ENV = {
    "EXPERIENCE_YEARS": "3.5",
    "SMTP_USER": "user@example.com",
    "SMTP_APP_PASSWORD": "app-password",
    "DIGEST_RECIPIENT": "user@example.com",
    "DEEPSEEK_API_KEY": "sk-deepseek",  # matches the default LLM_PROVIDER=deepseek
}


@pytest.fixture
def valid_env(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    for key, value in REQUIRED_ENV.items():
        monkeypatch.setenv(key, value)
    return tmp_path


def test_valid_env_loads(valid_env):
    settings = load_settings()
    assert settings.experience_years == 3.5
    assert settings.smtp_user == "user@example.com"
    assert settings.llm_provider == "deepseek"
    assert settings.llm_request_timeout_seconds == 120.0


def test_llm_request_timeout_seconds_overridable(monkeypatch, valid_env):
    monkeypatch.setenv("LLM_REQUEST_TIMEOUT_SECONDS", "30")
    settings = load_settings()
    assert settings.llm_request_timeout_seconds == 30.0


def test_missing_api_key_for_llm_provider_raises(monkeypatch, valid_env):
    monkeypatch.delenv("DEEPSEEK_API_KEY")
    with pytest.raises(ConfigError) as exc_info:
        load_settings()
    assert any("deepseek_api_key" in p for p in exc_info.value.problems)


def test_blank_api_key_treated_as_not_set(monkeypatch, valid_env):
    # FOO_API_KEY= (present but blank) parses as SecretStr(''), not None —
    # must not silently pass as "set".
    monkeypatch.setenv("LLM_PROVIDER", "anthropic")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "")
    with pytest.raises(ConfigError) as exc_info:
        load_settings()
    assert any("anthropic_api_key" in p for p in exc_info.value.problems)


def test_unknown_provider_raises(monkeypatch, valid_env):
    monkeypatch.setenv("LLM_PROVIDER", "notaprovider")
    with pytest.raises(ConfigError) as exc_info:
        load_settings()
    assert any("notaprovider" in p for p in exc_info.value.problems)


def test_llm_provider_is_case_insensitive(monkeypatch, valid_env):
    monkeypatch.setenv("LLM_PROVIDER", "DeepSeek")
    settings = load_settings()
    assert settings.llm_provider == "deepseek"


def test_dev_mode_skips_api_key_requirement(monkeypatch, valid_env):
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


def test_experience_years_zero_raises(monkeypatch, valid_env):
    monkeypatch.setenv("EXPERIENCE_YEARS", "0")
    with pytest.raises(ConfigError) as exc_info:
        load_settings()
    assert any("experience_years" in p for p in exc_info.value.problems)


def test_experience_years_negative_raises(monkeypatch, valid_env):
    monkeypatch.setenv("EXPERIENCE_YEARS", "-1")
    with pytest.raises(ConfigError) as exc_info:
        load_settings()
    assert any("experience_years" in p for p in exc_info.value.problems)


def test_experience_years_too_large_raises(monkeypatch, valid_env):
    monkeypatch.setenv("EXPERIENCE_YEARS", "75")
    with pytest.raises(ConfigError) as exc_info:
        load_settings()
    assert any("experience_years" in p for p in exc_info.value.problems)


def test_experience_years_accepts_fractional_value(monkeypatch, valid_env):
    monkeypatch.setenv("EXPERIENCE_YEARS", "3")
    settings = load_settings()
    assert settings.experience_years == 3.0


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
    monkeypatch.delenv("DEEPSEEK_API_KEY")
    with pytest.raises(ConfigError) as exc_info:
        load_settings()
    problems = exc_info.value.problems
    assert len(problems) >= 3
    assert any("score_threshold_consider" in p for p in problems)
    assert any("digest_time_pdt" in p for p in problems)
    assert any("deepseek_api_key" in p for p in problems)


def test_missing_required_field_raises_config_error(monkeypatch, valid_env):
    monkeypatch.delenv("SMTP_USER")
    with pytest.raises(ConfigError):
        load_settings()


# --- daemon / dashboard settings (Modules 20, 21) --------------------------


def test_daemon_and_dashboard_defaults(valid_env):
    settings = load_settings()
    assert settings.poll_interval_minutes == 30
    assert settings.poll_timeout_minutes == 1440
    assert settings.digest_timeout_minutes == 10
    assert settings.force_poll_hours == 6
    assert settings.dashboard_enabled is True
    # Loopback by default — the page has no auth, so exposing it must be a
    # deliberate act rather than something you get by accident.
    assert settings.dashboard_host == "127.0.0.1"
    assert settings.dashboard_port == 8787
    assert settings.log_level == "INFO"


@pytest.mark.parametrize(
    "name",
    ["POLL_INTERVAL_MINUTES", "POLL_TIMEOUT_MINUTES", "DIGEST_TIMEOUT_MINUTES", "FORCE_POLL_HOURS"],
)
def test_non_positive_daemon_intervals_are_rejected(valid_env, monkeypatch, name):
    monkeypatch.setenv(name, "0")
    with pytest.raises(ConfigError) as excinfo:
        load_settings()
    assert any("must be positive" in problem for problem in excinfo.value.problems)


@pytest.mark.parametrize("port", ["0", "70000"])
def test_out_of_range_dashboard_port_is_rejected(valid_env, monkeypatch, port):
    monkeypatch.setenv("DASHBOARD_PORT", port)
    with pytest.raises(ConfigError) as excinfo:
        load_settings()
    assert any("dashboard_port" in problem for problem in excinfo.value.problems)


def test_blank_dashboard_host_is_rejected(valid_env, monkeypatch):
    monkeypatch.setenv("DASHBOARD_HOST", "   ")
    with pytest.raises(ConfigError) as excinfo:
        load_settings()
    assert any("dashboard_host" in problem for problem in excinfo.value.problems)


def test_log_level_is_configurable(valid_env, monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "DEBUG")
    assert load_settings().log_level == "DEBUG"


def test_unknown_log_level_is_rejected(valid_env, monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "CHATTY")
    with pytest.raises(ConfigError) as excinfo:
        load_settings()
    assert any("log_level" in problem for problem in excinfo.value.problems)


def test_max_posting_age_days_default_and_validation(valid_env, monkeypatch):
    assert load_settings().max_posting_age_days == 15

    monkeypatch.setenv("MAX_POSTING_AGE_DAYS", "30")
    assert load_settings().max_posting_age_days == 30

    monkeypatch.setenv("MAX_POSTING_AGE_DAYS", "0")
    with pytest.raises(ConfigError) as excinfo:
        load_settings()
    assert any("max_posting_age_days" in p for p in excinfo.value.problems)
