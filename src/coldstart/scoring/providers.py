from __future__ import annotations

import anthropic
import openai
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types

from coldstart.logging_setup import get_logger
from coldstart.scoring.base import (
    LLMProvider,
    LLMResponse,
    LLMUsage,
    ProviderUnavailable,
    RateLimitError,
)
from coldstart.settings import Settings

logger = get_logger(__name__)

DEFAULT_TIMEOUT_SECONDS = 120.0

_OPENAI_COMPATIBLE_BASE_URLS: dict[str, str | None] = {
    "deepseek": "https://api.deepseek.com/v1",
    "kimi": "https://api.moonshot.ai/v1",
    "mistral": "https://api.mistral.ai/v1",
    "grok": "https://api.x.ai/v1",
    "openai": None,
    "ollama": "http://localhost:11434/v1",
}

_API_KEY_FIELDS = {
    "deepseek": "deepseek_api_key",
    "kimi": "kimi_api_key",
    "mistral": "mistral_api_key",
    "grok": "grok_api_key",
    "openai": "openai_api_key",
    "anthropic": "anthropic_api_key",
    "gemini": "gemini_api_key",
}

_MODEL_FIELDS = {
    "deepseek": "deepseek_model",
    "kimi": "kimi_model",
    "mistral": "mistral_model",
    "grok": "grok_model",
    "openai": "openai_model",
    "anthropic": "anthropic_model",
    "gemini": "gemini_model",
}

# Best-effort defaults as of this build — provider model lineups change
# constantly; re-verify against each provider's current catalog before
# relying on these for prod use. Override per-provider via settings (e.g.
# ANTHROPIC_MODEL in .env) rather than editing these directly.
DEFAULT_MODELS = {
    "deepseek": "deepseek-chat",
    "kimi": "moonshot-v1-8k",
    "mistral": "mistral-large-latest",
    "grok": "grok-4",
    "openai": "gpt-5-mini",
    "anthropic": "claude-sonnet-5",
    "gemini": "gemini-2.5-flash",
}

# USD per 1M tokens. Same staleness caveat as DEFAULT_MODELS above — these
# WILL drift from real provider pricing pages and must be re-verified
# periodically. estimate_cost() degrades to 0 + a warning on an unknown
# model rather than ever raising.
PRICING: dict[str, dict[str, float]] = {
    "deepseek-chat": {"input": 0.28, "cache_hit_input": 0.028, "output": 0.42},
    "moonshot-v1-8k": {"input": 0.20, "cache_hit_input": 0.02, "output": 2.00},
    "mistral-large-latest": {"input": 2.00, "cache_hit_input": 2.00, "output": 6.00},
    "grok-4": {"input": 3.00, "cache_hit_input": 0.75, "output": 15.00},
    "gpt-5-mini": {"input": 0.25, "cache_hit_input": 0.025, "output": 2.00},
    "claude-sonnet-5": {"input": 3.00, "cache_hit_input": 0.30, "output": 15.00},
    "gemini-2.5-flash": {"input": 0.30, "cache_hit_input": 0.075, "output": 2.50},
}


class UnknownProviderError(Exception):
    pass


class ProviderConfigError(Exception):
    """Missing API key or other required config for a requested provider."""


def estimate_cost(model: str, usage: LLMUsage) -> float:
    rates = PRICING.get(model)
    if rates is None:
        logger.warning("no pricing data for model %r, reporting cost as 0", model)
        return 0.0
    cache_hit = min(usage.cached_tokens, usage.input_tokens)
    cache_miss = max(usage.input_tokens - cache_hit, 0)
    return (
        cache_miss / 1_000_000 * rates["input"]
        + cache_hit / 1_000_000 * rates["cache_hit_input"]
        + usage.output_tokens / 1_000_000 * rates["output"]
    )


def _normalize_openai_usage(usage) -> LLMUsage:
    if usage is None:
        return LLMUsage(input_tokens=0, cached_tokens=0, output_tokens=0)
    # DeepSeek's disk-cache reports via a vendor-specific field not in the
    # standard OpenAI usage shape; everyone else (if they report it at all)
    # uses the nested prompt_tokens_details.cached_tokens field.
    cache_hit = getattr(usage, "prompt_cache_hit_tokens", None)
    if cache_hit is None and usage.prompt_tokens_details is not None:
        cache_hit = usage.prompt_tokens_details.cached_tokens
    return LLMUsage(
        input_tokens=usage.prompt_tokens or 0,
        cached_tokens=cache_hit or 0,
        output_tokens=usage.completion_tokens or 0,
    )


class OpenAICompatibleProvider(LLMProvider):
    """Shared implementation for DeepSeek, Kimi, Mistral, Grok, OpenAI, and
    Ollama — all speak the same /v1/chat/completions wire format, differing
    only in base_url/model/pricing/whether an API key is needed."""

    def __init__(
        self,
        *,
        name: str,
        model: str,
        api_key: str | None = None,
        base_url: str | None = None,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
    ):
        self.name = name
        self.model = model
        self._client = openai.OpenAI(
            api_key=api_key or "not-needed",
            base_url=base_url,
            timeout=timeout,
            max_retries=0,  # retry orchestration is Module 15's job, not the SDK's
        )

    def complete(
        self, system: str, user: str, *, json_schema: dict | None = None
    ) -> LLMResponse:
        try:
            response = self._client.chat.completions.create(
                model=self.model,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                response_format={"type": "json_object"},
            )
        except openai.RateLimitError as exc:
            raise RateLimitError(str(exc)) from exc
        except (openai.APIConnectionError, openai.InternalServerError) as exc:
            raise ProviderUnavailable(str(exc)) from exc

        choice = response.choices[0]
        text = choice.message.content or ""
        return LLMResponse(
            text=text,
            usage=_normalize_openai_usage(response.usage),
            provider=self.name,
            model=response.model or self.model,
        )

    def estimate_cost(self, usage: LLMUsage) -> float:
        return estimate_cost(self.model, usage)


def _normalize_anthropic_usage(usage) -> LLMUsage:
    cache_read = usage.cache_read_input_tokens or 0
    cache_creation = usage.cache_creation_input_tokens or 0
    # cache_creation (write) is billed at a premium over the base input rate,
    # not the base rate itself — folded into "fresh input" here as an MVP
    # simplification (slightly undercounts true cost on cache-writing calls).
    return LLMUsage(
        input_tokens=(usage.input_tokens or 0) + cache_creation + cache_read,
        cached_tokens=cache_read,
        output_tokens=usage.output_tokens or 0,
    )


class AnthropicProvider(LLMProvider):
    def __init__(self, *, api_key: str, model: str, timeout: float = DEFAULT_TIMEOUT_SECONDS):
        self.name = "anthropic"
        self.model = model
        self._client = anthropic.Anthropic(api_key=api_key, timeout=timeout, max_retries=0)

    def complete(
        self, system: str, user: str, *, json_schema: dict | None = None
    ) -> LLMResponse:
        try:
            response = self._client.messages.create(
                model=self.model,
                max_tokens=4096,
                # Explicit cache_control on the system block (scope.md §6.2) —
                # Anthropic doesn't cache automatically like DeepSeek/Kimi/Gemini.
                system=[
                    {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
                ],
                messages=[{"role": "user", "content": user}],
            )
        except anthropic.RateLimitError as exc:
            raise RateLimitError(str(exc)) from exc
        except (anthropic.APIConnectionError, anthropic.InternalServerError) as exc:
            raise ProviderUnavailable(str(exc)) from exc

        text = "".join(block.text for block in response.content if block.type == "text")
        return LLMResponse(
            text=text,
            usage=_normalize_anthropic_usage(response.usage),
            provider=self.name,
            model=response.model,
        )

    def estimate_cost(self, usage: LLMUsage) -> float:
        return estimate_cost(self.model, usage)


def _normalize_gemini_usage(usage_metadata) -> LLMUsage:
    if usage_metadata is None:
        return LLMUsage(input_tokens=0, cached_tokens=0, output_tokens=0)
    return LLMUsage(
        input_tokens=usage_metadata.prompt_token_count or 0,
        cached_tokens=usage_metadata.cached_content_token_count or 0,
        output_tokens=usage_metadata.candidates_token_count or 0,
    )


class GeminiProvider(LLMProvider):
    def __init__(self, *, api_key: str, model: str, timeout: float = DEFAULT_TIMEOUT_SECONDS):
        self.name = "gemini"
        self.model = model
        self._client = genai.Client(
            api_key=api_key,
            http_options=genai_types.HttpOptions(timeout=int(timeout * 1000)),
        )

    def complete(
        self, system: str, user: str, *, json_schema: dict | None = None
    ) -> LLMResponse:
        try:
            response = self._client.models.generate_content(
                model=self.model,
                contents=user,
                config=genai_types.GenerateContentConfig(
                    system_instruction=system,
                    response_mime_type="application/json",
                ),
            )
        except genai_errors.ClientError as exc:
            if exc.code == 429:
                raise RateLimitError(str(exc)) from exc
            raise
        except genai_errors.ServerError as exc:
            raise ProviderUnavailable(str(exc)) from exc

        text = response.text or ""
        return LLMResponse(
            text=text,
            usage=_normalize_gemini_usage(response.usage_metadata),
            provider=self.name,
            model=self.model,
        )

    def estimate_cost(self, usage: LLMUsage) -> float:
        return estimate_cost(self.model, usage)


def _resolve_model(name: str, settings: Settings) -> str:
    override = getattr(settings, _MODEL_FIELDS[name])
    return override or DEFAULT_MODELS[name]


def build_provider(name: str, settings: Settings) -> LLMProvider:
    timeout = settings.llm_request_timeout_seconds

    if name in _OPENAI_COMPATIBLE_BASE_URLS:
        if name == "ollama":
            if not settings.ollama_model:
                raise ProviderConfigError("ollama_model must be set to use the ollama provider")
            return OpenAICompatibleProvider(
                name=name,
                model=settings.ollama_model,
                api_key=None,
                base_url=settings.ollama_base_url,
                timeout=timeout,
            )

        key_field = _API_KEY_FIELDS[name]
        secret = getattr(settings, key_field)
        if secret is None:
            raise ProviderConfigError(f"{key_field} is not set for provider {name!r}")
        return OpenAICompatibleProvider(
            name=name,
            model=_resolve_model(name, settings),
            api_key=secret.get_secret_value(),
            base_url=_OPENAI_COMPATIBLE_BASE_URLS[name],
            timeout=timeout,
        )

    if name == "anthropic":
        secret = settings.anthropic_api_key
        if secret is None:
            raise ProviderConfigError("anthropic_api_key is not set")
        return AnthropicProvider(
            api_key=secret.get_secret_value(),
            model=_resolve_model("anthropic", settings),
            timeout=timeout,
        )

    if name == "gemini":
        secret = settings.gemini_api_key
        if secret is None:
            raise ProviderConfigError("gemini_api_key is not set")
        return GeminiProvider(
            api_key=secret.get_secret_value(),
            model=_resolve_model("gemini", settings),
            timeout=timeout,
        )

    raise UnknownProviderError(f"unknown provider: {name!r}")


def build_active_provider(settings: Settings) -> LLMProvider:
    """The one provider this run uses — no cross-provider fallback.
    LLM_MODE picks the branch: "dev" always means Ollama; "prod" means
    whichever single provider LLM_PROVIDER names."""
    name = "ollama" if settings.llm_mode == "dev" else settings.llm_provider
    return build_provider(name, settings)
