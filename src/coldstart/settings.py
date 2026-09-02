from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Literal

from pydantic import EmailStr, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

from coldstart.logging_setup import get_logger

logger = get_logger(__name__)

# "ollama" isn't here on purpose — LLM_MODE picks between Ollama (dev) and
# one of these (prod); LLM_PROVIDER only ever names a prod provider.
_KNOWN_PROD_PROVIDERS = {
    "deepseek",
    "kimi",
    "gemini",
    "mistral",
    "openai",
    "anthropic",
    "grok",
}

_PROVIDER_KEY_FIELDS = {
    "deepseek": "deepseek_api_key",
    "kimi": "kimi_api_key",
    "gemini": "gemini_api_key",
    "mistral": "mistral_api_key",
    "openai": "openai_api_key",
    "anthropic": "anthropic_api_key",
    "grok": "grok_api_key",
}

_MAX_EXPERIENCE_YEARS = 60
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


class ConfigError(Exception):
    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("Invalid configuration:\n" + "\n".join(f"  - {p}" for p in problems))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Experience — X in the YOE formula (scope.md §6.4), entered directly as a
    # number of years (e.g. 3.5) rather than derived from a start date, since
    # a career break would make date-based derivation silently overcount.
    # Single shared value for all 4 resumes (confirmed: no per-resume split).
    experience_years: float

    # LLM — LLM_MODE picks the branch: "dev" always uses Ollama (OLLAMA_MODEL
    # directly, no key needed); "prod" uses exactly one provider, named by
    # LLM_PROVIDER, with that provider's API key. No cross-provider fallback.
    llm_mode: Literal["prod", "dev"] = "prod"
    llm_provider: str = "deepseek"
    batch_size: int = 1
    max_retries_per_provider: int = 3
    ollama_model: str | None = None
    ollama_base_url: str = "http://localhost:11434/v1"
    llm_request_timeout_seconds: float = 120.0  # local Ollama is slow (Module 13)

    # API keys (all optional; validated only for providers actually in use)
    deepseek_api_key: SecretStr | None = None
    kimi_api_key: SecretStr | None = None
    gemini_api_key: SecretStr | None = None
    mistral_api_key: SecretStr | None = None
    openai_api_key: SecretStr | None = None
    anthropic_api_key: SecretStr | None = None
    grok_api_key: SecretStr | None = None

    # Per-provider model override — unset falls back to
    # scoring.providers.DEFAULT_MODELS (e.g. pick anthropic_model to choose
    # between Haiku/Sonnet/Opus rather than always using the built-in default).
    deepseek_model: str | None = None
    kimi_model: str | None = None
    gemini_model: str | None = None
    mistral_model: str | None = None
    openai_model: str | None = None
    anthropic_model: str | None = None
    grok_model: str | None = None

    # Budget
    daily_token_spend_ceiling_usd: float = 3.0

    # Freshness — skip postings older than this before they reach an LLM.
    # A job posted months ago is usually filled; scoring it is money spent on
    # something you can't apply to. Postings with no date are always kept.
    max_posting_age_days: int = 15

    # Scoring — binary: a job either clears the bar or is rejected. There is
    # no middle "worth considering" tier any more (dropped 2026-08-31 at the
    # operator's request; it sat unused between 60-70 and just added noise).
    score_threshold_strong: int = 80

    # Email
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: EmailStr
    smtp_app_password: SecretStr
    digest_recipient: EmailStr
    digest_time_pdt: str = "08:00"
    timezone: str = "America/Los_Angeles"

    # Logging — Module 1 specified LOG_LEVEL from .env; it was declared as a
    # setup_logging() parameter but no setting ever fed it, so every run was
    # hardcoded to INFO. A daemon runs for weeks, so this actually matters now.
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"

    # Paths
    data_dir: Path = Path("data")
    log_dir: Path = Path("logs")
    output_dir: Path = Path("output")
    resume_manifest: Path = Path("config/resumes/manifest.json")
    db_path: Path = Path("data/coldstart.sqlite3")

    # Polling
    manifest_url: str = "https://storage.stapply.ai/jobhive/v1/manifest.json"
    poll_interval_minutes: int = 30

    # Daemon (Module 20). `poll_interval_minutes` above and `digest_time_pdt`
    # were dead settings until the daemon landed — nothing read either.
    # Runaway guard, not a target. Sized for the worst case rather than the
    # common one: a first backfill walks all 36 slices and legitimately runs
    # for many hours. 240 was too low and killed a real backfill four hours
    # in. A kill is cheap either way — slice_state means the next run skips
    # every slice that finished — but it wastes the in-flight slice's work.
    poll_timeout_minutes: int = 1440
    digest_timeout_minutes: int = 10
    force_poll_hours: int = 6

    # Dashboard (Module 21). Loopback by default — the page has no auth, so
    # binding it to 0.0.0.0 must be a deliberate act, never the default.
    dashboard_enabled: bool = True
    dashboard_host: str = "127.0.0.1"
    dashboard_port: int = 8787

    # Liveness check (Module 27). Verifies a posting still exists at the
    # source ATS (workday/greenhouse/lever — see verify.py) before an LLM
    # call is spent on it, and periodically re-checks already-scored postings
    # the operator hasn't acted on yet, since a posting can die between being
    # scored and being looked at. `_enabled` is one switch for both stages —
    # a real HTTP call per checkable job against a third-party site is the
    # kind of thing an operator should be able to turn off in one place.
    liveness_check_enabled: bool = True
    liveness_sweep_interval_hours: int = 24
    liveness_sweep_timeout_minutes: int = 15

    @field_validator("llm_provider", mode="before")
    @classmethod
    def _normalize_provider(cls, value: object) -> object:
        if isinstance(value, str):
            return value.strip().lower()
        return value


def _validate(settings: Settings) -> list[str]:
    problems: list[str] = []

    if settings.experience_years <= 0:
        problems.append(f"experience_years ({settings.experience_years}) must be positive")
    elif settings.experience_years > _MAX_EXPERIENCE_YEARS:
        problems.append(
            f"experience_years ({settings.experience_years}) is above "
            f"{_MAX_EXPERIENCE_YEARS}, which looks like a mistake"
        )

    if settings.llm_mode == "dev":
        if not settings.ollama_model:
            problems.append("ollama_model must be set when llm_mode='dev'")
    elif settings.llm_provider not in _KNOWN_PROD_PROVIDERS:
        problems.append(
            f"unknown llm_provider {settings.llm_provider!r} — must be one of "
            f"{sorted(_KNOWN_PROD_PROVIDERS)}"
        )
    else:
        key_field = _PROVIDER_KEY_FIELDS[settings.llm_provider]
        secret = getattr(settings, key_field)
        # `FOO_API_KEY=` (present but blank) parses as SecretStr(''), not None
        # — `is None` alone doesn't catch it, and this would otherwise pass
        # validation only to fail later with a confusing auth error instead.
        if secret is None or not secret.get_secret_value().strip():
            problems.append(
                f"llm_provider is {settings.llm_provider!r} but {key_field} is not set"
            )

    if not _TIME_RE.match(settings.digest_time_pdt):
        problems.append(f"digest_time_pdt ({settings.digest_time_pdt!r}) is not in HH:MM format")

    for name, value in (
        ("poll_interval_minutes", settings.poll_interval_minutes),
        ("poll_timeout_minutes", settings.poll_timeout_minutes),
        ("digest_timeout_minutes", settings.digest_timeout_minutes),
        ("force_poll_hours", settings.force_poll_hours),
        ("liveness_sweep_interval_hours", settings.liveness_sweep_interval_hours),
        ("liveness_sweep_timeout_minutes", settings.liveness_sweep_timeout_minutes),
    ):
        if value <= 0:
            problems.append(f"{name} ({value}) must be positive")

    if settings.max_posting_age_days <= 0:
        problems.append(
            f"max_posting_age_days ({settings.max_posting_age_days}) must be positive"
        )

    if not 1 <= settings.dashboard_port <= 65535:
        problems.append(f"dashboard_port ({settings.dashboard_port}) must be between 1 and 65535")

    if not settings.dashboard_host.strip():
        problems.append("dashboard_host must not be blank")

    problems.extend(_validate_resume_manifest(settings.resume_manifest))

    for name, dir_path in (
        ("data_dir", settings.data_dir),
        ("log_dir", settings.log_dir),
        ("output_dir", settings.output_dir),
    ):
        try:
            dir_path.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            problems.append(f"{name} ({dir_path}) is not creatable: {exc}")

    return problems


def _validate_resume_manifest(manifest_path: Path) -> list[str]:
    if not manifest_path.exists():
        return []

    problems: list[str] = []
    try:
        manifest = json.loads(manifest_path.read_text())
    except (json.JSONDecodeError, OSError) as exc:
        return [f"resume_manifest ({manifest_path}) is not valid JSON: {exc}"]

    for resume_id, entry in manifest.items():
        file_str = entry.get("file") if isinstance(entry, dict) else None
        if not file_str:
            problems.append(f"resume_manifest entry {resume_id!r} has no 'file' field")
            continue
        resume_path = Path(file_str)
        if not resume_path.is_absolute():
            resume_path = manifest_path.parent / resume_path
        if not resume_path.exists():
            problems.append(f"resume file for {resume_id!r} does not exist: {resume_path}")
        elif resume_path.stat().st_size == 0:
            problems.append(f"resume file for {resume_id!r} is empty: {resume_path}")

    return problems


def load_settings() -> Settings:
    try:
        settings = Settings()
    except ValidationError as exc:
        problems = [f"{'.'.join(str(p) for p in err['loc'])}: {err['msg']}" for err in exc.errors()]
        logger.critical("Invalid configuration:\n%s", "\n".join(f"  - {p}" for p in problems))
        raise ConfigError(problems) from exc

    problems = _validate(settings)
    if problems:
        logger.critical("Invalid configuration:\n%s", "\n".join(f"  - {p}" for p in problems))
        raise ConfigError(problems)

    return settings
