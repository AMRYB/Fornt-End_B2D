"""LLM provider backed by the Cursor Cloud Agents API.

``crsr_*`` API keys authenticate to https://api.cursor.com/v1 which exposes an
agentic Cloud Agents API rather than a plain chat-completions endpoint. This
provider creates a short-lived *no-repo* agent with the combined prompt,
polls its run to completion and returns the final assistant reply text.

No secrets are logged. Agent names never include sensitive content.
"""

from __future__ import annotations

import asyncio
import json
import time
import uuid

import httpx

from ..config import Settings
from .base import LLMProvider, LLMProviderError

_DEFAULT_MAX_ERROR_BODY = 500

# Statuses that mean "the model has started generating".
_RUNNING_STATUSES = {"RUNNING", "IN_PROGRESS", "GENERATING", "FINISHED"}


class CursorCloudProvider(LLMProvider):
    def __init__(self, settings: Settings):
        self._api_key = settings.effective_api_key()
        self._base_url = settings.llm_base_url.rstrip("/")
        self._model = settings.llm_model
        self._request_timeout = settings.llm_request_timeout_s
        self._poll_interval = settings.llm_poll_interval_s
        self._poll_timeout = settings.llm_poll_timeout_s
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
        stats["model"] = self._model if self._model != "default" else "cursor-default"
        stats.setdefault("started_at", time.strftime("%Y-%m-%dT%H:%M:%S%z"))
        prompt_text = f"{system_prompt}\n\n{user_prompt}".strip()
        agent_id, run_id, initial_status = await self._create_agent(prompt_text, stats)
        try:
            result = await self._await_run(agent_id, run_id, initial_status, started, stats)
            stats["total_s"] = stats.get("total_s", 0.0) + (time.monotonic() - started)
            return result
        finally:
            await self._archive_agent(agent_id)

    async def _create_agent(
        self, prompt_text: str, stats: dict
    ) -> tuple[str, str, str]:
        body: dict = {"prompt": {"text": prompt_text}}
        if self._model and self._model != "default":
            body["model"] = {"id": self._model}
        try:
            response = await self._client.post(
                "/agents",
                headers=self._auth_headers(),
                json=body,
            )
        except httpx.HTTPError as exc:
            raise LLMProviderError(f"Cursor API request failed: {exc}") from exc
        if response.is_error:
            raise self._to_error(response)
        data = response.json()
        agent_id = data.get("agent", {}).get("id")
        run_id = data.get("run", {}).get("id")
        if not agent_id or not run_id:
            raise LLMProviderError("Cursor API did not return agent/run ids")
        initial_status = str(data.get("run", {}).get("status") or "")
        return agent_id, run_id, initial_status

    async def _await_run(
        self,
        agent_id: str,
        run_id: str,
        initial_status: str,
        started: float,
        stats: dict,
    ) -> str:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._poll_timeout
        # TTFT proxy for a poll-based API: the run was already generating when
        # the create response returned (TTFT ~ 0), otherwise record the elapsed
        # time until the run first leaves its queued state.
        ttft_recorded = initial_status in _RUNNING_STATUSES
        if ttft_recorded:
            stats["ttft_s"] = 0.0
        while True:
            try:
                run = await self._get_run(agent_id, run_id)
            except httpx.HTTPError as exc:
                raise LLMProviderError(f"Cursor API poll failed: {exc}") from exc
            status = run.get("status")
            if not ttft_recorded and status in _RUNNING_STATUSES:
                stats["ttft_s"] = round(time.monotonic() - started, 3)
                ttft_recorded = True
            if status == "FINISHED":
                result = run.get("result") or ""
                if not result.strip():
                    raise LLMProviderError("Cursor agent finished with an empty result")
                if not ttft_recorded:
                    stats["ttft_s"] = round(time.monotonic() - started, 3)
                return result
            if status in ("ERROR", "CANCELLED", "EXPIRED"):
                raise LLMProviderError(f"Cursor agent run ended with status={status}")
            if loop.time() > deadline:
                raise LLMProviderError("Timed out waiting for Cursor agent run")
            await asyncio.sleep(self._poll_interval)

    async def _get_run(self, agent_id: str, run_id: str) -> dict:
        response = await self._client.get(
            f"/agents/{agent_id}/runs/{run_id}",
            headers=self._auth_headers(),
        )
        if response.is_error:
            raise self._to_error(response)
        return response.json()

    async def _archive_agent(self, agent_id: str) -> None:
        try:
            await self._client.post(
                f"/agents/{agent_id}/archive",
                headers=self._auth_headers(),
            )
        except httpx.HTTPError:
            pass

    def _auth_headers(self) -> dict[str, str]:
        return {"Authorization": f"Bearer {self._api_key}"}

    def _to_error(self, response: httpx.Response) -> LLMProviderError:
        body = response.text.strip() or ""
        detail = ""
        if body:
            try:
                payload = json.loads(body)
                detail = str(payload.get("message") or payload)
            except json.JSONDecodeError:
                detail = body[: _DEFAULT_MAX_ERROR_BODY]
        return LLMProviderError(
            f"Cursor API error {response.status_code}: {detail[: _DEFAULT_MAX_ERROR_BODY]}"
        )