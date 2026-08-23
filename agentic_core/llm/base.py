"""LLM provider abstraction.

The entire agentic system depends on this interface instead of a concrete
provider, so the underlying model/service can be replaced without touching
any agent code.
"""

from __future__ import annotations

import inspect
from abc import ABC, abstractmethod


class LLMProviderError(Exception):
    """Raised when the underlying LLM provider fails (network, auth, etc.)."""


class LLMGenerationError(Exception):
    """Raised when the provider returns unusable output."""


class StructuredOutputError(Exception):
    """Raised when structured output cannot be parsed/validated."""


class LLMProvider(ABC):
    """Stateless interface to an LLM: given prompts, return assistant text.

    An optional ``stats`` dict may be passed for telemetry; providers update it
    in place (call_id, model, time-to-first-token, total duration, …).
    """

    @abstractmethod
    async def generate(
        self, system_prompt: str, user_prompt: str, stats: dict | None = None
    ) -> str:
        """Generate a single assistant response as plain text."""

    async def aclose(self) -> None:
        """Release any underlying resources."""


class FakeLLMProvider(LLMProvider):
    """In-memory provider for tests and offline demos."""

    def __init__(self, responses: list[str] | None = None, handler=None):
        self._responses = list(responses or [])
        self._handler = handler
        self.calls: list[tuple[str, str]] = []

    def set_handler(self, handler) -> None:
        self._handler = handler

    def set_responses(self, responses: list[str]) -> None:
        self._responses = list(responses)

    async def generate(
        self, system_prompt: str, user_prompt: str, stats: dict | None = None
    ) -> str:
        self.calls.append((system_prompt, user_prompt))
        if stats is not None:
            stats.setdefault("call_id", "fake")
            stats.setdefault("model", "fake")
            stats["ttft_s"] = 0.0
            stats["total_s"] = 0.0
        if self._handler is not None:
            result = self._handler(system_prompt, user_prompt)
            if inspect.isawaitable(result):
                result = await result
            return result
        if self._responses:
            return self._responses.pop(0)
        raise LLMProviderError("FakeLLMProvider has no response configured")