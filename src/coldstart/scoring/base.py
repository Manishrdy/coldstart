from __future__ import annotations

from abc import ABC, abstractmethod

from pydantic import BaseModel


class LLMUsage(BaseModel):
    input_tokens: int
    cached_tokens: int
    output_tokens: int


class LLMResponse(BaseModel):
    text: str
    usage: LLMUsage
    provider: str
    model: str


class RateLimitError(Exception):
    """Raised on HTTP 429 from a provider."""


class ProviderUnavailable(Exception):
    """Raised on 5xx / network-level failure from a provider."""


class InvalidResponse(Exception):
    """Raised when a provider's response can't be parsed."""


class LLMProvider(ABC):
    """Common interface across all LLM providers (scope.md §6.1).

    Prompt-caching contract (scope.md §6.2): `system` must hold the stable
    prefix (instructions + resume text) and `user` the variable JD —
    implementations must not reorder these. DeepSeek/Kimi/Gemini cache
    automatically on a stable prefix; Anthropic needs explicit
    cache_control on the system block to get the same benefit.
    """

    name: str

    @abstractmethod
    def complete(
        self, system: str, user: str, *, json_schema: dict | None = None
    ) -> LLMResponse: ...

    @abstractmethod
    def estimate_cost(self, usage: LLMUsage) -> float: ...
