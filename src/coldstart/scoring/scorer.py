from __future__ import annotations

import json
import sqlite3
import time

from pydantic import ValidationError
from tenacity import Retrying, retry_if_exception_type, stop_after_attempt, wait_exponential

from coldstart.budget import check_budget, record_spend
from coldstart.errors import log_error
from coldstart.logging_setup import get_logger
from coldstart.models import JobScore, RawJob
from coldstart.scoring.base import (
    InvalidResponse,
    LLMProvider,
    LLMResponse,
    ProviderUnavailable,
    RateLimitError,
)
from coldstart.scoring.rubric import build_system_prompt, build_user_prompt
from coldstart.settings import Settings
from coldstart.text_utils import strip_markdown_json_fences

logger = get_logger(__name__)

_CORRECTION_SUFFIX = (
    "\n\nYour previous response did not match the required JSON schema. "
    "Return only valid JSON matching the schema described above."
)
_MAX_SCHEMA_ATTEMPTS = 2  # initial attempt + one correction retry


def _complete_with_retry(
    provider: LLMProvider, system: str, user: str, max_retries: int
) -> LLMResponse:
    retrying = Retrying(
        retry=retry_if_exception_type((RateLimitError, ProviderUnavailable)),
        stop=stop_after_attempt(max_retries),
        wait=wait_exponential(multiplier=1, min=1, max=30),
        reraise=True,
    )
    return retrying(provider.complete, system, user)


def _parse_job_score(text: str) -> JobScore:
    cleaned = strip_markdown_json_fences(text)
    data = json.loads(cleaned)
    return JobScore.model_validate(data)


def _parse_job_score_batch(text: str, expected_count: int) -> list[JobScore]:
    cleaned = strip_markdown_json_fences(text)
    data = json.loads(cleaned)
    if not isinstance(data, list) or len(data) != expected_count:
        got = len(data) if isinstance(data, list) else type(data).__name__
        raise InvalidResponse(f"expected a JSON array of {expected_count} objects, got {got}")
    return [JobScore.model_validate(item) for item in data]


def _score_with_provider(
    job_ref: str,
    system: str,
    user: str,
    provider: LLMProvider,
    conn: sqlite3.Connection,
    settings: Settings,
) -> JobScore | None:
    prompt = user
    for attempt in range(_MAX_SCHEMA_ATTEMPTS):
        start = time.monotonic()
        try:
            response = _complete_with_retry(
                provider, system, prompt, settings.max_retries_per_provider
            )
        except (RateLimitError, ProviderUnavailable) as exc:
            logger.error(
                "provider %s exhausted retries for %s: %s", provider.name, job_ref, exc
            )
            return None
        elapsed = time.monotonic() - start

        cost = provider.estimate_cost(response.usage)
        record_spend(conn, provider.name, response.model, response.usage, cost, job_ref)

        cache_ratio = (
            response.usage.cached_tokens / response.usage.input_tokens
            if response.usage.input_tokens
            else 0.0
        )
        logger.info(
            "llm call: job=%s provider=%s model=%s latency=%.2fs tokens_in=%d "
            "cached=%d (%.0f%%) tokens_out=%d cost=$%.5f",
            job_ref,
            provider.name,
            response.model,
            elapsed,
            response.usage.input_tokens,
            response.usage.cached_tokens,
            cache_ratio * 100,
            response.usage.output_tokens,
            cost,
        )

        try:
            score = _parse_job_score(response.text)
        except (json.JSONDecodeError, ValidationError) as exc:
            logger.warning(
                "schema-correction retry: job=%s provider=%s attempt=%d: %s",
                job_ref,
                provider.name,
                attempt + 1,
                exc,
            )
            prompt = user + _CORRECTION_SUFFIX
            continue

        logger.info(
            "scored: job=%s provider=%s score=%d band=%s",
            job_ref,
            provider.name,
            score.score,
            score.score_band.value,
        )
        return score

    logger.error(
        "provider %s exhausted schema-correction retries for %s", provider.name, job_ref
    )
    return None


def score_job(
    job: RawJob,
    resume_text: str,
    provider: LLMProvider,
    conn: sqlite3.Connection,
    settings: Settings,
) -> JobScore | None:
    check_budget(conn, settings)  # raises BudgetExceeded uncaught -> stops the whole run

    system = build_system_prompt(resume_text, settings.experience_years)
    user = build_user_prompt(job)

    score = _score_with_provider(job.global_id, system, user, provider, conn, settings)
    if score is not None:
        return score

    log_error(
        conn,
        stage="llm_score",
        message=f"provider {provider.name!r} exhausted for job {job.global_id!r}",
        source_file=__name__,
        function_name="score_job",
        provider=provider.name,
        job_ref=job.global_id,
    )
    logger.error(
        "provider %s exhausted for job %s — will be marked FAILED", provider.name, job.global_id
    )
    return None


def _score_batch_with_provider(
    jobs: list[RawJob],
    system: str,
    provider: LLMProvider,
    conn: sqlite3.Connection,
    settings: Settings,
) -> list[JobScore] | None:
    batch_ref = f"batch[{jobs[0].global_id}..{jobs[-1].global_id}]"
    sections = [f"=== Job {i + 1} ===\n{build_user_prompt(job)}" for i, job in enumerate(jobs)]
    # Overrides the system prompt's single-object framing — a known rough
    # edge left for when batch_size actually gets used (default is 1).
    instruction = (
        f"\n\nThere are {len(jobs)} jobs above. Respond with a JSON ARRAY of "
        f"exactly {len(jobs)} objects, one per job in the same order, each "
        "matching the schema described above."
    )
    prompt = "\n\n".join(sections) + instruction

    for attempt in range(_MAX_SCHEMA_ATTEMPTS):
        try:
            response = _complete_with_retry(
                provider, system, prompt, settings.max_retries_per_provider
            )
        except (RateLimitError, ProviderUnavailable) as exc:
            logger.error(
                "provider %s exhausted retries for %s: %s", provider.name, batch_ref, exc
            )
            return None

        cost = provider.estimate_cost(response.usage)
        record_spend(conn, provider.name, response.model, response.usage, cost, batch_ref)

        try:
            return _parse_job_score_batch(response.text, expected_count=len(jobs))
        except (json.JSONDecodeError, ValidationError, InvalidResponse) as exc:
            logger.warning(
                "schema-correction retry: batch=%s provider=%s attempt=%d: %s",
                batch_ref,
                provider.name,
                attempt + 1,
                exc,
            )
            prompt = prompt + _CORRECTION_SUFFIX

    logger.error(
        "provider %s exhausted schema-correction retries for %s", provider.name, batch_ref
    )
    return None


def score_jobs(
    jobs: list[RawJob],
    resume_text: str,
    provider: LLMProvider,
    conn: sqlite3.Connection,
    settings: Settings,
    batch_size: int = 1,
) -> list[tuple[RawJob, JobScore | None]]:
    results: list[tuple[RawJob, JobScore | None]] = []

    if batch_size <= 1:
        for job in jobs:
            results.append((job, score_job(job, resume_text, provider, conn, settings)))
    else:
        system = build_system_prompt(resume_text, settings.experience_years)
        for start in range(0, len(jobs), batch_size):
            chunk = jobs[start : start + batch_size]
            check_budget(conn, settings)

            scores = _score_batch_with_provider(chunk, system, provider, conn, settings)
            if scores is None:
                log_error(
                    conn,
                    stage="llm_score",
                    message=f"provider {provider.name!r} exhausted for batch "
                    f"starting at {chunk[0].global_id!r}",
                    source_file=__name__,
                    function_name="score_jobs",
                    provider=provider.name,
                    job_ref=chunk[0].global_id,
                )
                results.extend((job, None) for job in chunk)
            else:
                results.extend(zip(chunk, scores, strict=True))

    total = len(results)
    scored = sum(1 for _, s in results if s is not None)
    by_band: dict[str, int] = {}
    for _, s in results:
        if s is not None:
            by_band[s.score_band.value] = by_band.get(s.score_band.value, 0) + 1
    logger.info(
        "score_jobs summary: total=%d scored=%d failed=%d by_band=%s",
        total,
        scored,
        total - scored,
        by_band,
    )
    return results
