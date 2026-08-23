"""Google Gemini provider using the native ``generateContent`` REST API."""

from __future__ import annotations

import asyncio
import json
import time
import uuid
from email.utils import parsedate_to_datetime
from typing import Any

import httpx

from ..config import Settings
from .base import LLMProvider, LLMProviderError


class GeminiProvider(LLMProvider):
    """Generate structured agent output with one explicitly assigned API key."""

    def __init__(self, settings: Settings, api_key: str | None = None):
        self._api_key = (api_key or settings.effective_api_key()).strip()
        if not self._api_key:
            raise RuntimeError("GeminiProvider requires a non-empty API key")
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
        if stats is None:
            stats = {}
        started = time.monotonic()
        stats["call_id"] = uuid.uuid4().hex[:12]
        stats["model"] = self._model
        stats["provider"] = "gemini"
        stats.setdefault("started_at", time.strftime("%Y-%m-%dT%H:%M:%S%z"))

        last_error: Exception | None = None
        for attempt in range(self._request_retries + 1):
            try:
                response = await self._client.post(
                    f"/models/{self._model}:generateContent",
                    headers={
                        "x-goog-api-key": self._api_key,
                        "Content-Type": "application/json",
                    },
                    json={
                        "system_instruction": {
                            "parts": [{"text": system_prompt}],
                        },
                        "contents": [
                            {
                                "role": "user",
                                "parts": [{"text": user_prompt}],
                            }
                        ],
                        "generationConfig": {
                            "responseMimeType": "application/json",
                            "maxOutputTokens": self._max_tokens,
                        },
                    },
                )
                if response.is_error:
                    error = self._to_error(response)
                    if self._is_retryable(response.status_code) and attempt < self._request_retries:
                        stats["retry_count"] = attempt + 1
                        await asyncio.sleep(self._retry_delay(response, attempt))
                        last_error = error
                        continue
                    raise error

                payload = response.json()
                content = self._extract_content(payload)
                usage = payload.get("usageMetadata") or {}
                stats["input_tokens"] = usage.get("promptTokenCount", 0)
                stats["output_tokens"] = usage.get("candidatesTokenCount", 0)
                stats["total_tokens"] = usage.get("totalTokenCount", 0)
                stats["retry_count"] = attempt
                stats["ttft_s"] = 0.0
                stats["total_s"] = stats.get("total_s", 0.0) + (
                    time.monotonic() - started
                )
                stats["provider_completion_latency_s"] = stats["total_s"]
                return content
            except (httpx.HTTPError, ValueError) as exc:
                last_error = exc
                if attempt >= self._request_retries:
                    raise LLMProviderError(
                        f"Gemini API request failed: {exc}"
                    ) from exc
                stats["retry_count"] = attempt + 1
                await asyncio.sleep(0.25 * (2**attempt))
            except LLMProviderError:
                raise

        raise LLMProviderError(
            f"Gemini API request failed: {last_error or 'no usable result'}"
        )

    @staticmethod
    def _extract_content(payload: dict[str, Any]) -> str:
        candidates = payload.get("candidates") or []
        if not candidates:
            feedback = payload.get("promptFeedback") or {}
            raise ValueError(f"Gemini response had no candidates: {feedback}")
        content = candidates[0].get("content") or {}
        parts = content.get("parts") or []
        text = "".join(
            str(part["text"])
            for part in parts
            if isinstance(part, dict) and part.get("text")
        )
        if not text:
            finish_reason = candidates[0].get("finishReason", "unknown")
            raise ValueError(
                f"Gemini response contained no text (finish reason: {finish_reason})"
            )
        return text

    @staticmethod
    def _is_retryable(status_code: int) -> bool:
        return status_code in {408, 429} or status_code >= 500

    @staticmethod
    def _retry_delay(response: httpx.Response, attempt: int) -> float:
        value = response.headers.get("Retry-After", "").strip()
        if value:
            try:
                return min(max(float(value), 0.0), 30.0)
            except ValueError:
                try:
                    retry_at = parsedate_to_datetime(value)
                    return min(max(retry_at.timestamp() - time.time(), 0.0), 30.0)
                except (TypeError, ValueError, OverflowError):
                    pass
        return min(0.25 * (2**attempt), 4.0)

    @staticmethod
    def _to_error(response: httpx.Response) -> LLMProviderError:
        detail = ""
        body = response.text.strip()
        if body:
            try:
                payload = json.loads(body)
                error = payload.get("error") or payload
                if isinstance(error, dict):
                    detail = str(error.get("message") or error)
                else:
                    detail = str(error)
            except json.JSONDecodeError:
                detail = body[:500]
        return LLMProviderError(
            f"Gemini API error {response.status_code}: {detail[:500]}"
        )
