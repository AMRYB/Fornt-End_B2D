from __future__ import annotations

import json

import httpx

from agentic_core.config import Settings
from agentic_core.llm import OpenRouterProvider, create_llm_provider



def test_openrouter_settings_defaults_and_factory(tmp_path):
    settings = Settings(
        llm_provider="openrouter",
        llm_model="default",
        openrouter_api_key="test-openrouter-key",
        data_dir=tmp_path,
    )

    assert settings.effective_model() == "moonshotai/kimi-k2.6:free"
    assert settings.effective_base_url() == "https://openrouter.ai/api/v1"
    assert isinstance(create_llm_provider(settings), OpenRouterProvider)


async def test_openrouter_generates_from_chat_completion(tmp_path):
    settings = Settings(
        llm_provider="openrouter",
        llm_model="moonshotai/kimi-k2.6:free",
        openrouter_api_key="test-openrouter-key",
        data_dir=tmp_path,
    )
    provider = OpenRouterProvider(settings)
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": '{"status":"ready"}'}}]},
        )

    await provider._client.aclose()
    provider._client = httpx.AsyncClient(
        base_url="https://openrouter.ai/api/v1",
        transport=httpx.MockTransport(handler),
    )
    stats = {"started_at": "test"}
    try:
        result = await provider.generate("system", "user", stats)
    finally:
        await provider.aclose()

    assert result == '{"status":"ready"}'
    assert stats["provider"] == "openrouter"
    assert stats["model"] == "moonshotai/kimi-k2.6:free"
    assert requests[0].url.path == "/api/v1/chat/completions"
    assert requests[0].headers["Authorization"] == "Bearer test-openrouter-key"
    body = json.loads(requests[0].content)
    assert body["model"] == "moonshotai/kimi-k2.6:free"
    assert body["max_tokens"] == 8192
