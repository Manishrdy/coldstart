from __future__ import annotations

import json
import re
from datetime import date
from pathlib import Path
from typing import Annotated, Literal

from pydantic import EmailStr, SecretStr, ValidationError, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from coldstart.logging_setup import get_logger

logger = get_logger(__name__)

_KNOWN_PROVIDERS = {
    "deepseek",
    "kimi",
    "gemini",
    "mistral",
    "openai",
    "anthropic",
    "grok",
    "ollama",
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

_MIN_EXPERIENCE_YEAR = 2015
_TIME_RE = re.compile(r"^([01]\d|2[0-3]):([0-5]\d)$")


class ConfigError(Exception):
    def __init__(self, problems: list[str]):
        self.problems = problems
        super().__init__("Invalid configuration:\n" + "\n".join(f"  - {p}" for p in problems))


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    # Experience — single shared YOE anchor for all 4 resumes (confirmed: no per-resume split)
    experience_start_date: date

    # LLM
    llm_mode: Literal["prod", "dev"] = "prod"
    llm_provider: str = "deepseek"
    provider_fallback_order: Annotated[list[str], NoDecode] = ["deepseek", "kimi"]
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

    # Budget
    daily_token_spend_ceiling_usd: float = 3.0

    # Scoring
    score_threshold_strong: int = 70
    score_threshold_consider: int = 60

    # Email
    smtp_host: str = "smtp.gmail.com"
    smtp_port: int = 587
    smtp_user: EmailStr
    smtp_app_password: SecretStr
    digest_recipient: EmailStr
    digest_time_pdt: str = "08:00"
    timezone: str = "America/Los_Angeles"

    # Paths
    data_dir: Path = Path("data")
    log_dir: Path = Path("logs")
    output_dir: Path = Path("output")
    resume_manifest: Path = Path("config/resumes/manifest.json")
    db_path: Path = Path("data/coldstart.sqlite3")

    # Polling
    manifest_url: str = "https://storage.stapply.ai/jobhive/v1/manifest.json"
    poll_interval_minutes: int = 30

    @field_validator("provider_fallback_order", mode="before")
    @classmethod
    def _split_csv(cls, value: object) -> object:
        if isinstance(value, str):
            return [item.strip() for item in value.split(",") if item.strip()]
        return value


def years_of_experience(start: date, today: date | None = None) -> float:
    today = today or date.today()
    return (today - start).days / 365.25


def _validate(settings: Settings) -> list[str]:
    problems: list[str] = []
    today = date.today()

    if settings.experience_start_date >= today:
        problems.append(
            f"experience_start_date ({settings.experience_start_date}) must be in the past"
        )
    elif settings.experience_start_date.year < _MIN_EXPERIENCE_YEAR:
        problems.append(
            f"experience_start_date ({settings.experience_start_date}) is before "
            f"{_MIN_EXPERIENCE_YEAR}, which looks like a mistake"
        )

    unknown_providers = {p for p in settings.provider_fallback_order if p not in _KNOWN_PROVIDERS}
    for provider in sorted(unknown_providers):
        problems.append(f"unknown provider {provider!r} in provider_fallback_order")

    if settings.llm_mode == "dev":
        if not settings.ollama_model:
            problems.append("ollama_model must be set when llm_mode='dev'")
    else:
        for provider in settings.provider_fallback_order:
            if provider == "ollama" or provider in unknown_providers:
                continue
            key_field = _PROVIDER_KEY_FIELDS[provider]
            if getattr(settings, key_field) is None:
                problems.append(
                    f"provider {provider!r} is in provider_fallback_order but "
                    f"{key_field} is not set"
                )

    if settings.score_threshold_consider >= settings.score_threshold_strong:
        problems.append(
            f"score_threshold_consider ({settings.score_threshold_consider}) must be less "
            f"than score_threshold_strong ({settings.score_threshold_strong})"
        )

    if not _TIME_RE.match(settings.digest_time_pdt):
        problems.append(f"digest_time_pdt ({settings.digest_time_pdt!r}) is not in HH:MM format")

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
