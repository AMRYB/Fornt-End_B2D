"""Moonshot / Kimi provider using the OpenAI-compatible chat-completions API."""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

import httpx

from ..config import Settings
from .base import LLMProvider, LLMProviderError


class KimiProvider(LLMProvider):
    """OpenAI-compatible provider for Moonshot Kimi models.

    The implementation intentionally mirrors the existing Cursor provider's
    contract: one async generate() call, stats mutation for telemetry, and a
    bounded retry loop without infinite polling or sleep-based backoff.
    """

    def __init__(self, settings: Settings):
        self._provider_name = "kimi"
        self._api_key = settings.effective_api_key()
        self._base_url = settings.effective_base_url().rstrip("/")
        self._model = settings.effective_model()
        self._request_timeout = settings.llm_request_timeout_s
        self._max_tokens = settings.llm_max_tokens
        self._request_retries = max(0, int(settings.max_llm_retries))
        self._client = httpx.AsyncClient(
            base_url=self._base_url,
            timeout=self._request_timeout,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    async def generate(
        self, system_prompt: str, user_prompt: str, stats: dict | None = None
    ) -> str:
        stats = stats or {}
        started = time.monotonic()
        stats["call_id"] = uuid.uuid4().hex[:12]
        stats["model"] = self._model
        stats["provider"] = self._provider_name
        stats.setdefault("started_at", time.strftime("%Y-%m-%dT%H:%M:%S%z"))

        prompt_text = f"{system_prompt}\n\n{user_prompt}".strip()
        last_error: Exception | None = None

        for attempt in range(self._request_retries + 1):
            try:
                response = await self._client.post(
                    "/chat/completions",
                    headers={
                        "Authorization": f"Bearer {self._api_key}",
                        "Content-Type": "application/json",
                    },
                    json={
                        "model": self._model,
                        "max_tokens": self._max_tokens,
                        "messages": [
                            {"role": "system", "content": system_prompt},
                            {"role": "user", "content": user_prompt},
                        ],
                    },
                )
                if response.is_error:
                    raise self._to_error(response)
                payload = response.json()
                content = self._extract_content(payload)
                if not content:
                    raise LLMProviderError("Kimi response did not contain assistant content")
                stats["total_s"] = stats.get("total_s", 0.0) + (time.monotonic() - started)
                stats["retry_count"] = attempt
                stats["ttft_s"] = 0.0
                # Non-streaming completion API: report completion latency instead of
                # fake TTFT. A real streaming TTFT would require incremental deltas.
                stats["provider_completion_latency_s"] = stats["total_s"]
                return content
            except (httpx.HTTPError, LLMProviderError, ValueError) as exc:
                last_error = exc
                if attempt >= self._request_retries:
                    raise LLMProviderError(f"Kimi API request failed: {exc}") from exc
                stats["retry_count"] = attempt + 1
                continue

        if last_error is not None:
            raise LLMProviderError(f"Kimi API request failed: {last_error}") from last_error
        raise LLMProviderError("Kimi API request failed with no usable result")

    def _extract_content(self, payload: dict[str, Any]) -> str:
        choices = payload.get("choices") or []
        if not choices:
            raise ValueError("Kimi response had no choices")
        first = choices[0]
        message = first.get("message") or {}
        content = message.get("content")
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict):
                    text = item.get("text")
                    if text:
                        parts.append(str(text))
            return "".join(parts)
        if isinstance(content, str):
            return content
        raise ValueError("Kimi response did not contain message content")

    def _to_error(self, response: httpx.Response) -> LLMProviderError:
        body = response.text.strip() or ""
        detail = ""
        if body:
            try:
                payload = json.loads(body)
                detail = str(payload.get("error") or payload)
            except json.JSONDecodeError:
                detail = body[:500]
        return LLMProviderError(
            f"Kimi API error {response.status_code}: {detail[:500]}"
        )
