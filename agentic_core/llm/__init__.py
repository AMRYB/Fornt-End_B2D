"""LLM layer: provider abstraction, concrete providers, service and fake."""

from ..config import GEMINI_AGENT_NAMES
from .base import (
    FakeLLMProvider,
    LLMGenerationError,
    LLMProvider,
    LLMProviderError,
    StructuredOutputError,
)
from .cursor_provider import CursorCloudProvider
from .gemini_provider import GeminiProvider
from .kimi_provider import KimiProvider
from .openrouter_provider import OpenRouterProvider
from .service import LLMService, extract_json_object


def create_llm_provider(settings, *, api_key: str | None = None):
    """Construct the provider selected in configuration."""
    provider_name = (settings.llm_provider or "cursor").strip().lower()
    if provider_name == "cursor":
        return CursorCloudProvider(settings)
    if provider_name == "kimi":
        return KimiProvider(settings)
    if provider_name == "openrouter":
        return OpenRouterProvider(settings)
    if provider_name == "gemini":
        return GeminiProvider(settings, api_key=api_key)
    raise RuntimeError(f"Unsupported LLM provider: {provider_name!r}")


def create_agent_llm_services(settings) -> dict[str, LLMService]:
    """Build isolated Gemini services, or one shared service for other providers."""
    if settings.effective_provider() == "gemini":
        settings.check_credentials()
        return {
            name: LLMService(
                create_llm_provider(
                    settings,
                    api_key=settings.gemini_api_key_for(name),
                ),
                settings,
            )
            for name in GEMINI_AGENT_NAMES
        }

    shared = LLMService(create_llm_provider(settings), settings)
    return {name: shared for name in GEMINI_AGENT_NAMES}


async def aclose_llm_services(services) -> None:
    """Close each provider behind a service mapping exactly once."""
    providers = {id(service.provider): service.provider for service in services.values()}
    for provider in providers.values():
        await provider.aclose()


__all__ = [
    "CursorCloudProvider",
    "FakeLLMProvider",
    "GeminiProvider",
    "KimiProvider",
    "LLMGenerationError",
    "LLMProvider",
    "LLMProviderError",
    "LLMService",
    "OpenRouterProvider",
    "StructuredOutputError",
    "aclose_llm_services",
    "create_agent_llm_services",
    "create_llm_provider",
    "extract_json_object",
]