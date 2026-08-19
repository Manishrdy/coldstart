import logging
from types import SimpleNamespace

import anthropic
import httpx
import openai
import pytest
from google.genai import errors as genai_errors

from coldstart.scoring.base import LLMUsage, ProviderUnavailable, RateLimitError
from coldstart.scoring.providers import (
    DEFAULT_MODELS,
    PRICING,
    AnthropicProvider,
    GeminiProvider,
    OpenAICompatibleProvider,
    ProviderConfigError,
    UnknownProviderError,
    build_fallback_chain,
    build_provider,
    estimate_cost,
)
from coldstart.settings import Settings


def _settings(**overrides) -> Settings:
    kwargs = dict(
        experience_years=3.5,
        smtp_user="user@example.com",
        smtp_app_password="app-pw",
        digest_recipient="user@example.com",
    )
    kwargs.update(overrides)
    return Settings(**kwargs)


def _openai_error(cls, status: int):
    request = httpx.Request("POST", "https://api.example.com/v1/chat/completions")
    return cls(f"error {status}", response=httpx.Response(status, request=request), body=None)


def _fake_openai_completion(
    text='{"a": 1}',
    prompt_tokens=100,
    completion_tokens=10,
    cached_tokens=20,
    model="deepseek-chat",
    prompt_cache_hit_tokens=None,
):
    details = SimpleNamespace(cached_tokens=cached_tokens) if cached_tokens is not None else None
    usage = SimpleNamespace(
        prompt_tokens=prompt_tokens,
        completion_tokens=completion_tokens,
        total_tokens=prompt_tokens + completion_tokens,
        prompt_tokens_details=details,
    )
    if prompt_cache_hit_tokens is not None:
        usage.prompt_cache_hit_tokens = prompt_cache_hit_tokens
    message = SimpleNamespace(content=text)
    choice = SimpleNamespace(message=message)
    return SimpleNamespace(choices=[choice], usage=usage, model=model)


# --- OpenAICompatibleProvider --------------------------------------------------


def test_openai_compatible_success(monkeypatch):
    provider = OpenAICompatibleProvider(
        name="deepseek", model="deepseek-chat", api_key="fake", base_url="https://api.deepseek.com/v1"
    )
    monkeypatch.setattr(
        provider._client.chat.completions,
        "create",
        lambda **kw: _fake_openai_completion(text='{"score": 80}'),
    )
    response = provider.complete("system prompt", "user prompt")
    assert response.text == '{"score": 80}'
    assert response.provider == "deepseek"
    assert response.model == "deepseek-chat"
    assert response.usage == LLMUsage(input_tokens=100, cached_tokens=20, output_tokens=10)


def test_openai_compatible_deepseek_cache_hit_field_preferred(monkeypatch):
    provider = OpenAICompatibleProvider(name="deepseek", model="deepseek-chat", api_key="fake")
    monkeypatch.setattr(
        provider._client.chat.completions,
        "create",
        lambda **kw: _fake_openai_completion(cached_tokens=999, prompt_cache_hit_tokens=15),
    )
    response = provider.complete("s", "u")
    assert response.usage.cached_tokens == 15


def test_openai_compatible_missing_usage_defaults_to_zero(monkeypatch):
    provider = OpenAICompatibleProvider(name="deepseek", model="deepseek-chat", api_key="fake")
    completion = _fake_openai_completion()
    completion.usage = None
    monkeypatch.setattr(provider._client.chat.completions, "create", lambda **kw: completion)
    response = provider.complete("s", "u")
    assert response.usage == LLMUsage(input_tokens=0, cached_tokens=0, output_tokens=0)


def test_openai_compatible_429_maps_to_rate_limit_error(monkeypatch):
    provider = OpenAICompatibleProvider(name="deepseek", model="deepseek-chat", api_key="fake")

    def _raise(**kw):
        raise _openai_error(openai.RateLimitError, 429)

    monkeypatch.setattr(provider._client.chat.completions, "create", _raise)
    with pytest.raises(RateLimitError):
        provider.complete("s", "u")


def test_openai_compatible_500_maps_to_provider_unavailable(monkeypatch):
    provider = OpenAICompatibleProvider(name="deepseek", model="deepseek-chat", api_key="fake")

    def _raise(**kw):
        raise _openai_error(openai.InternalServerError, 500)

    monkeypatch.setattr(provider._client.chat.completions, "create", _raise)
    with pytest.raises(ProviderUnavailable):
        provider.complete("s", "u")


def test_openai_compatible_connection_error_maps_to_provider_unavailable(monkeypatch):
    provider = OpenAICompatibleProvider(name="deepseek", model="deepseek-chat", api_key="fake")

    def _raise(**kw):
        raise openai.APIConnectionError(request=httpx.Request("POST", "https://x"))

    monkeypatch.setattr(provider._client.chat.completions, "create", _raise)
    with pytest.raises(ProviderUnavailable):
        provider.complete("s", "u")


def test_ollama_needs_no_api_key():
    provider = OpenAICompatibleProvider(
        name="ollama", model="qwen2.5:3b", api_key=None, base_url="http://localhost:11434/v1"
    )
    assert provider.name == "ollama"


# --- AnthropicProvider ---------------------------------------------------------


def _fake_anthropic_message(
    text="hello",
    input_tokens=50,
    output_tokens=10,
    cache_read=5,
    cache_creation=0,
    model="claude-sonnet-5",
):
    block = SimpleNamespace(type="text", text=text)
    usage = SimpleNamespace(
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cache_read_input_tokens=cache_read,
        cache_creation_input_tokens=cache_creation,
    )
    return SimpleNamespace(content=[block], usage=usage, model=model)


def test_anthropic_success(monkeypatch):
    provider = AnthropicProvider(api_key="fake", model="claude-sonnet-5")
    monkeypatch.setattr(
        provider._client.messages, "create", lambda **kw: _fake_anthropic_message(text='{"a":1}')
    )
    response = provider.complete("system prompt", "user prompt")
    assert response.text == '{"a":1}'
    assert response.provider == "anthropic"
    assert response.usage.input_tokens == 55  # 50 fresh + 5 cache_read + 0 cache_creation
    assert response.usage.cached_tokens == 5


def test_anthropic_sends_cache_control_on_system_block(monkeypatch):
    provider = AnthropicProvider(api_key="fake", model="claude-sonnet-5")
    captured = {}

    def _capture(**kwargs):
        captured.update(kwargs)
        return _fake_anthropic_message()

    monkeypatch.setattr(provider._client.messages, "create", _capture)
    provider.complete("stable system prefix", "variable JD")
    assert captured["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert captured["system"][0]["text"] == "stable system prefix"
    assert captured["messages"][0]["content"] == "variable JD"


def test_anthropic_429_maps_to_rate_limit_error(monkeypatch):
    provider = AnthropicProvider(api_key="fake", model="claude-sonnet-5")
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")

    def _raise(**kw):
        raise anthropic.RateLimitError(
            "rate limited", response=httpx.Response(429, request=request), body=None
        )

    monkeypatch.setattr(provider._client.messages, "create", _raise)
    with pytest.raises(RateLimitError):
        provider.complete("s", "u")


def test_anthropic_500_maps_to_provider_unavailable(monkeypatch):
    provider = AnthropicProvider(api_key="fake", model="claude-sonnet-5")
    request = httpx.Request("POST", "https://api.anthropic.com/v1/messages")

    def _raise(**kw):
        raise anthropic.InternalServerError(
            "server error", response=httpx.Response(500, request=request), body=None
        )

    monkeypatch.setattr(provider._client.messages, "create", _raise)
    with pytest.raises(ProviderUnavailable):
        provider.complete("s", "u")


# --- GeminiProvider --------------------------------------------------------------


def _fake_gemini_response(text='{"a":1}', prompt_tokens=40, cached=8, candidates_tokens=12):
    usage_metadata = SimpleNamespace(
        prompt_token_count=prompt_tokens,
        cached_content_token_count=cached,
        candidates_token_count=candidates_tokens,
    )
    return SimpleNamespace(text=text, usage_metadata=usage_metadata)


def test_gemini_success(monkeypatch):
    provider = GeminiProvider(api_key="fake", model="gemini-2.5-flash")
    monkeypatch.setattr(
        provider._client.models, "generate_content", lambda **kw: _fake_gemini_response()
    )
    response = provider.complete("system prompt", "user prompt")
    assert response.text == '{"a":1}'
    assert response.provider == "gemini"
    assert response.usage == LLMUsage(input_tokens=40, cached_tokens=8, output_tokens=12)


def test_gemini_429_maps_to_rate_limit_error(monkeypatch):
    provider = GeminiProvider(api_key="fake", model="gemini-2.5-flash")

    def _raise(**kw):
        raise genai_errors.ClientError(code=429, response_json={})

    monkeypatch.setattr(provider._client.models, "generate_content", _raise)
    with pytest.raises(RateLimitError):
        provider.complete("s", "u")


def test_gemini_other_client_error_propagates_unchanged(monkeypatch):
    provider = GeminiProvider(api_key="fake", model="gemini-2.5-flash")

    def _raise(**kw):
        raise genai_errors.ClientError(code=400, response_json={})

    monkeypatch.setattr(provider._client.models, "generate_content", _raise)
    with pytest.raises(genai_errors.ClientError):
        provider.complete("s", "u")


def test_gemini_500_maps_to_provider_unavailable(monkeypatch):
    provider = GeminiProvider(api_key="fake", model="gemini-2.5-flash")

    def _raise(**kw):
        raise genai_errors.ServerError(code=500, response_json={})

    monkeypatch.setattr(provider._client.models, "generate_content", _raise)
    with pytest.raises(ProviderUnavailable):
        provider.complete("s", "u")


def test_gemini_missing_usage_metadata_defaults_to_zero(monkeypatch):
    provider = GeminiProvider(api_key="fake", model="gemini-2.5-flash")
    monkeypatch.setattr(
        provider._client.models,
        "generate_content",
        lambda **kw: SimpleNamespace(text="x", usage_metadata=None),
    )
    response = provider.complete("s", "u")
    assert response.usage == LLMUsage(input_tokens=0, cached_tokens=0, output_tokens=0)


def test_openai_compatible_provider_estimate_cost_method():
    provider = OpenAICompatibleProvider(name="deepseek", model="deepseek-chat", api_key="fake")
    usage = LLMUsage(input_tokens=1000, cached_tokens=0, output_tokens=100)
    assert provider.estimate_cost(usage) > 0


def test_anthropic_provider_estimate_cost_method():
    provider = AnthropicProvider(api_key="fake", model="claude-sonnet-5")
    usage = LLMUsage(input_tokens=1000, cached_tokens=0, output_tokens=100)
    assert provider.estimate_cost(usage) > 0


def test_gemini_provider_estimate_cost_method():
    provider = GeminiProvider(api_key="fake", model="gemini-2.5-flash")
    usage = LLMUsage(input_tokens=1000, cached_tokens=0, output_tokens=100)
    assert provider.estimate_cost(usage) > 0


# --- estimate_cost ---------------------------------------------------------------


def test_estimate_cost_known_model_math():
    rates = PRICING["deepseek-chat"]
    usage = LLMUsage(input_tokens=1_000_000, cached_tokens=200_000, output_tokens=500_000)
    cost = estimate_cost("deepseek-chat", usage)
    expected = (
        800_000 / 1e6 * rates["input"]
        + 200_000 / 1e6 * rates["cache_hit_input"]
        + 500_000 / 1e6 * rates["output"]
    )
    assert cost == pytest.approx(expected)


def test_estimate_cost_unknown_model_returns_zero_and_warns(caplog):
    usage = LLMUsage(input_tokens=100, cached_tokens=0, output_tokens=10)
    with caplog.at_level(logging.WARNING, logger="coldstart.scoring.providers"):
        cost = estimate_cost("some-unreleased-model-xyz", usage)
    assert cost == 0.0
    assert any("no pricing data" in r.getMessage() for r in caplog.records)


def test_estimate_cost_cached_tokens_capped_at_input_tokens():
    # defensive: cached_tokens should never exceed input_tokens in real data,
    # but the math must not go negative/wrong if it somehow does
    usage = LLMUsage(input_tokens=100, cached_tokens=500, output_tokens=10)
    cost = estimate_cost("deepseek-chat", usage)
    assert cost >= 0


# --- build_provider / build_fallback_chain ---------------------------------------


@pytest.mark.parametrize(
    "name,key_field",
    [
        ("deepseek", "deepseek_api_key"),
        ("kimi", "kimi_api_key"),
        ("mistral", "mistral_api_key"),
        ("grok", "grok_api_key"),
        ("openai", "openai_api_key"),
        ("anthropic", "anthropic_api_key"),
        ("gemini", "gemini_api_key"),
    ],
)
def test_build_provider_with_key_succeeds(name, key_field):
    settings = _settings(**{key_field: "fake-key-value"})
    provider = build_provider(name, settings)
    assert provider.name == name
    assert provider.model == DEFAULT_MODELS[name]


@pytest.mark.parametrize(
    "name,key_field,model_field",
    [
        ("deepseek", "deepseek_api_key", "deepseek_model"),
        ("kimi", "kimi_api_key", "kimi_model"),
        ("mistral", "mistral_api_key", "mistral_model"),
        ("grok", "grok_api_key", "grok_model"),
        ("openai", "openai_api_key", "openai_model"),
        ("anthropic", "anthropic_api_key", "anthropic_model"),
        ("gemini", "gemini_api_key", "gemini_model"),
    ],
)
def test_build_provider_model_override_takes_precedence(name, key_field, model_field):
    settings = _settings(**{key_field: "fake-key-value", model_field: "custom-model-id"})
    provider = build_provider(name, settings)
    assert provider.model == "custom-model-id"


@pytest.mark.parametrize(
    "name,key_field",
    [
        ("deepseek", "deepseek_api_key"),
        ("mistral", "mistral_api_key"),
        ("anthropic", "anthropic_api_key"),
        ("gemini", "gemini_api_key"),
    ],
)
def test_build_provider_missing_key_raises_clear_error(name, key_field):
    settings = _settings()
    with pytest.raises(ProviderConfigError, match=key_field):
        build_provider(name, settings)


def test_build_provider_ollama_needs_no_key():
    settings = _settings(ollama_model="qwen2.5:3b")
    provider = build_provider("ollama", settings)
    assert provider.name == "ollama"


def test_build_provider_ollama_missing_model_raises():
    settings = _settings()
    with pytest.raises(ProviderConfigError, match="ollama_model"):
        build_provider("ollama", settings)


def test_build_provider_unknown_name_raises():
    settings = _settings(deepseek_api_key="k")
    with pytest.raises(UnknownProviderError):
        build_provider("not-a-real-provider", settings)


def test_build_fallback_chain_builds_all_providers_in_order():
    settings = _settings(
        deepseek_api_key="k1", kimi_api_key="k2", provider_fallback_order="deepseek,kimi"
    )
    chain = build_fallback_chain(settings)
    assert [p.name for p in chain] == ["deepseek", "kimi"]
