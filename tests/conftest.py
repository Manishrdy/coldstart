from __future__ import annotations

from coldstart.scoring.base import LLMProvider, LLMResponse, LLMUsage


class FakeProvider(LLMProvider):
    """Test double satisfying the LLMProvider ABC. Queue up text responses
    (or exception instances) and it returns/raises them in order, one per
    .complete() call, wrapping plain strings in an LLMResponse automatically
    so callers can keep supplying just the text content they want back."""

    def __init__(self, responses: list[str | Exception], *, name: str = "fake"):
        self._responses = list(responses)
        self.calls = 0
        self.name = name

    def complete(
        self, system: str, user: str, *, json_schema: dict | None = None
    ) -> LLMResponse:
        self.calls += 1
        response = self._responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return LLMResponse(
            text=response,
            usage=LLMUsage(input_tokens=10, cached_tokens=0, output_tokens=5),
            provider=self.name,
            model="fake-model",
        )

    def estimate_cost(self, usage: LLMUsage) -> float:
        return 0.0
