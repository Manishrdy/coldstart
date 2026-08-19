import pytest
from conftest import FakeProvider
from pydantic import ValidationError

from coldstart.scoring.base import (
    InvalidResponse,
    LLMProvider,
    LLMResponse,
    LLMUsage,
    ProviderUnavailable,
    RateLimitError,
)


def test_fake_provider_satisfies_interface():
    provider = FakeProvider(["hello"], name="fake")
    response = provider.complete("system prompt", "user prompt")
    assert isinstance(response, LLMResponse)
    assert response.text == "hello"
    assert response.provider == "fake"
    assert provider.estimate_cost(response.usage) == 0.0


def test_fake_provider_can_raise_queued_exceptions():
    provider = FakeProvider([RateLimitError("429")])
    with pytest.raises(RateLimitError):
        provider.complete("s", "u")


def test_incomplete_subclass_cannot_be_instantiated():
    class IncompleteProvider(LLMProvider):
        name = "incomplete"

        def complete(self, system, user, *, json_schema=None):
            raise NotImplementedError

        # estimate_cost intentionally not implemented

    with pytest.raises(TypeError):
        IncompleteProvider()


def test_llm_usage_fields():
    usage = LLMUsage(input_tokens=100, cached_tokens=20, output_tokens=50)
    assert usage.input_tokens == 100
    assert usage.cached_tokens == 20
    assert usage.output_tokens == 50


def test_llm_response_requires_usage():
    with pytest.raises(ValidationError):
        LLMResponse(text="x", provider="p", model="m")


def test_exception_types_are_distinct():
    assert issubclass(RateLimitError, Exception)
    assert issubclass(ProviderUnavailable, Exception)
    assert issubclass(InvalidResponse, Exception)
    assert not issubclass(RateLimitError, ProviderUnavailable)
