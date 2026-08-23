from __future__ import annotations

import json

import httpx

from agentic_core.config import Settings
from agentic_core.llm import GeminiProvider, create_llm_provider


def gemini_settings(tmp_path, **overrides) -> Settings:
    values = {
        "llm_provider": "gemini",
        "gemini_discovery_api_key": "key-discovery",
        "gemini_requirements_api_key": "key-requirements",
        "gemini_architecture_api_key": "key-architecture",
        "gemini_database_api_key": "key-database",
        "gemini_api_api_key": "key-api",
        "gemini_devops_api_key": "key-devops",
        "gemini_reviewer_api_key": "key-reviewer",
        "data_dir": tmp_path,
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


async def test_gemini_settings_defaults_and_factory(tmp_path):
    settings = gemini_settings(tmp_path)

    assert settings.effective_model() == "gemini-2.5-flash"
    assert (
        settings.effective_base_url()
        == "https://generativelanguage.googleapis.com/v1beta"
    )
    provider = create_llm_provider(settings)
    try:
        assert isinstance(provider, GeminiProvider)
    finally:
        await provider.aclose()


async def test_gemini_generates_from_generate_content(tmp_path):
    settings = gemini_settings(tmp_path)
    provider = GeminiProvider(settings, api_key="dedicated-agent-key")
    requests: list[httpx.Request] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {"content": {"parts": [{"text": '{"status":"ready"}'}]}}
                ],
                "usageMetadata": {
                    "promptTokenCount": 10,
                    "candidatesTokenCount": 4,
                    "totalTokenCount": 14,
                },
            },
        )

    await provider._client.aclose()
    provider._client = httpx.AsyncClient(
        base_url=settings.effective_base_url(),
        transport=httpx.MockTransport(handler),
    )
    stats: dict = {}
    try:
        result = await provider.generate("system", "user", stats)
    finally:
        await provider.aclose()

    assert result == '{"status":"ready"}'
    assert stats["provider"] == "gemini"
    assert stats["model"] == "gemini-2.5-flash"
    assert stats["input_tokens"] == 10
    assert stats["output_tokens"] == 4
    assert requests[0].url.path == (
        "/v1beta/models/gemini-2.5-flash:generateContent"
    )
    assert requests[0].headers["x-goog-api-key"] == "dedicated-agent-key"
    body = json.loads(requests[0].content)
    assert body["system_instruction"]["parts"][0]["text"] == "system"
    assert body["contents"][0]["parts"][0]["text"] == "user"
    assert body["generationConfig"]["responseMimeType"] == "application/json"
    assert body["generationConfig"]["maxOutputTokens"] == 8192


async def test_gemini_retries_rate_limit_response(tmp_path):
    settings = gemini_settings(tmp_path, max_llm_retries=1)
    provider = GeminiProvider(settings, api_key="dedicated-agent-key")
    attempts = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            return httpx.Response(
                429,
                headers={"Retry-After": "0"},
                json={"error": {"message": "rate limited"}},
            )
        return httpx.Response(
            200,
            json={
                "candidates": [
                    {"content": {"parts": [{"text": '{"status":"ready"}'}]}}
                ]
            },
        )

    await provider._client.aclose()
    provider._client = httpx.AsyncClient(
        base_url=settings.effective_base_url(),
        transport=httpx.MockTransport(handler),
    )
    stats: dict = {}
    try:
        result = await provider.generate("system", "user", stats)
    finally:
        await provider.aclose()

    assert result == '{"status":"ready"}'
    assert attempts == 2
    assert stats["retry_count"] == 1
