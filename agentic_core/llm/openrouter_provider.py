"""OpenRouter provider using its OpenAI-compatible chat-completions API."""

from __future__ import annotations

from .kimi_provider import KimiProvider


class OpenRouterProvider(KimiProvider):
    """OpenRouter transport with the shared chat-completions implementation."""

    def __init__(self, settings):
        super().__init__(settings)
        self._provider_name = "openrouter"
