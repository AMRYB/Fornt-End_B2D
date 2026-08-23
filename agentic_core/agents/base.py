"""Reusable agent abstraction.

Agents never talk to the frontend, never contain workflow logic and never touch
an LLM client directly — they go through :class:`LLMService`. Execution is
tracked through the optional :class:`ExecutionTracker`.
"""

from __future__ import annotations

import asyncio
import json
import time
from abc import ABC, abstractmethod
from contextvars import ContextVar
from typing import Any, Literal

from pydantic import BaseModel, Field

from ..llm import LLMGenerationError, LLMProviderError, LLMService
from ..schemas import ProjectContext


class AgentResult(BaseModel):
    agent: str
    status: Literal["success", "failed"] = "failed"
    output: dict[str, Any] | None = None
    output_model: Any | None = Field(default=None, exclude=True)
    raw_text: str = ""
    retry_count: int = 0
    duration_ms: int = 0
    error: str | None = None
    # Rough prompt/output sizes (chars) for cost visibility; tokens ~ chars/4.
    input_chars: int = 0
    output_chars: int = 0
    # Per-call LLM telemetry (from the provider/service).
    call_id: str = ""
    provider: str = ""
    model: str = ""
    ttft_s: float = 0.0
    total_s: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0
    # True when the failure was provider/transport-level (network, poll timeout,
    # auth) and a fresh full run is likely to succeed. Structured-output
    # failures already exhausted internal repair retries, so re-running the
    # whole agent just doubles cost without a materially better outcome.
    retryable: bool = False


class RevisionInstruction(BaseModel):
    """Targeted-regeneration context: the existing artifact plus the exact
    reviewer issues it must resolve. Regenerating an artifact is a *revision*,
    never a from-scratch redesign."""

    artifact: str
    existing: dict[str, Any]
    issues: list[dict[str, Any]] = Field(default_factory=list)


_CONTEXT_FIELDS: dict[str, str] = {
    "problem": "problem",
    "target_users": "target_users",
    "user_roles": "user_roles",
    "business_goals": "business_goals",
    "core_features": "core_features",
    "scope": "scope",
    "constraints": "constraints",
    "assumptions": "assumptions",
    "integrations": "integrations",
    "security_requirements": "security_requirements",
    "performance_requirements": "performance_requirements",
    "deployment_requirements": "deployment_requirements",
    "technology_preferences": "technology_preferences",
    "auth_requirement": "auth_requirement",
    "authorization_requirement": "authorization_requirement",
    "payment_requirement": "payment_requirement",
    "notification_requirement": "notification_requirement",
}


def project_context_payload(context: ProjectContext, *, include_transcript: bool = False) -> dict[str, Any]:
    """Serializable snapshot of the user-visible project context."""
    payload: dict[str, Any] = {
        "project_id": context.project_id,
        "business_idea": context.business_idea,
    }
    for field in _CONTEXT_FIELDS:
        payload[field] = getattr(context, field)
    if include_transcript:
        payload["conversation"] = [
            {"role": turn.role, "message": turn.message} for turn in context.transcript
        ]
    return payload


def revision_instruction_text(revision: RevisionInstruction) -> str:
    """Targeted-revision block appended to an agent's normal prompt.

    The agent sees its existing artifact plus only the reviewer issues it must
    fix, and is told to preserve every valid decision — the opposite of a
    from-scratch regeneration, which is what caused the non-converging review
    loop (each re-run re-randomized decisions and re-broke consistency).
    """
    issues_text = json.dumps(revision.issues, separators=(",", ":"), ensure_ascii=False)
    existing_text = json.dumps(
        revision.existing, separators=(",", ":"), ensure_ascii=False
    )
    return (
        f"\n\nREVISION TASK — {revision.artifact.upper()}\n"
        f"You are REVISING an existing {revision.artifact} artifact. Resolve ONLY "
        f"the reviewer issues listed below.\n\n"
        f"Existing {revision.artifact} artifact (preserve everything valid):\n"
        f"{existing_text}\n\n"
        f"Reviewer issues to resolve:\n{issues_text}\n\n"
        f"REVISION RULES\n"
        f"- Modify the existing artifact ONLY to resolve these issues.\n"
        f"- Preserve all valid existing decisions.\n"
        f"- Do NOT regenerate the artifact from scratch.\n"
        f"- Do NOT introduce new inconsistencies with the upstream artifacts above.\n"
        f"- Return ONLY a single valid JSON object matching the schema above."
    )


class BaseAgent(ABC):
    name: str = ""
    system_prompt: str = ""
    output_schema: type[BaseModel] | None = None

    def __init__(self, llm_service: LLMService, tracker=None):
        self._llm = llm_service
        self._tracker = tracker
        # One agent instance may serve concurrent Vercel requests. ContextVar
        # keeps telemetry isolated per asyncio task without changing every
        # concrete agent's ``self._stats`` usage.
        self.__stats_var: ContextVar[dict[str, Any] | None] = ContextVar(
            f"agent_stats_{self.name}_{id(self)}", default=None
        )
        self._stats: dict[str, Any] = {"repair_count": 0}

    @property
    def _stats(self) -> dict[str, Any]:
        stats = self.__stats_var.get()
        if stats is None:
            stats = {"repair_count": 0}
            self.__stats_var.set(stats)
        return stats

    @_stats.setter
    def _stats(self, value: dict[str, Any]) -> None:
        self.__stats_var.set(value)

    async def run(
        self,
        context: ProjectContext,
        revision: RevisionInstruction | None = None,
    ) -> AgentResult:
        """Execute the agent against a project context and track the run.

        ``revision`` is the targeted-regeneration instruction used when the
        orchestrator revises an existing artifact (never a from-scratch run).
        """
        self._stats = {"repair_count": 0}
        started = time.perf_counter()
        record = None
        if self._tracker is not None:
            # Persistence telemetry must never delay or fail the user's model
            # call.  Production tracking uses a synchronous PostgREST client,
            # so keep it off the event loop and treat it as best-effort.
            try:
                record = await asyncio.to_thread(
                    self._tracker.start,
                    self.name,
                    context.project_id,
                    self._input_snapshot(context),
                )
            except Exception:  # noqa: BLE001 - telemetry is non-critical
                record = None
        try:
            model = await self._execute(context, revision)
            elapsed = int((time.perf_counter() - started) * 1000)
            raw_text = json.dumps(model.model_dump())
            output_chars = len(raw_text)
            result = AgentResult(
                agent=self.name,
                status="success",
                output=model.model_dump(),
                output_model=model,
                raw_text=raw_text,
                retry_count=self._stats["repair_count"],
                duration_ms=elapsed,
                input_chars=self._stats.get("prompt_chars", 0),
                output_chars=output_chars,
                call_id=self._stats.get("call_id", ""),
                provider=self._stats.get("provider", ""),
                model=self._stats.get("model", ""),
                ttft_s=self._stats.get("ttft_s", 0.0),
                total_s=self._stats.get("total_s", 0.0),
                input_tokens=self._stats.get(
                    "input_tokens", self._stats.get("prompt_chars", 0) // 4
                ),
                output_tokens=self._stats.get("output_tokens", output_chars // 4),
                total_tokens=self._stats.get(
                    "total_tokens",
                    self._stats.get("prompt_chars", 0) // 4 + output_chars // 4,
                ),
            )
        except Exception as exc:  # noqa: BLE001 - report any agent failure
            elapsed = int((time.perf_counter() - started) * 1000)
            result = AgentResult(
                agent=self.name,
                status="failed",
                error=str(exc)[:2000],
                retry_count=self._stats["repair_count"],
                duration_ms=elapsed,
                input_chars=self._stats.get("prompt_chars", 0),
                output_chars=self._stats.get("output_chars", 0),
                call_id=self._stats.get("call_id", ""),
                provider=self._stats.get("provider", ""),
                model=self._stats.get("model", ""),
                ttft_s=self._stats.get("ttft_s", 0.0),
                total_s=self._stats.get("total_s", 0.0),
                input_tokens=self._stats.get(
                    "input_tokens", self._stats.get("prompt_chars", 0) // 4
                ),
                output_tokens=self._stats.get(
                    "output_tokens", self._stats.get("output_chars", 0) // 4
                ),
                total_tokens=self._stats.get(
                    "total_tokens",
                    self._stats.get("prompt_chars", 0) // 4
                    + self._stats.get("output_chars", 0) // 4,
                ),
                retryable=isinstance(exc, (LLMProviderError, LLMGenerationError)),
            )
        if self._tracker is not None and record is not None:
            try:
                await asyncio.to_thread(self._tracker.complete, record, result)
            except Exception:  # noqa: BLE001 - never discard a valid LLM result
                pass
        return result

    @abstractmethod
    async def _execute(
        self,
        context: ProjectContext,
        revision: RevisionInstruction | None = None,
    ) -> BaseModel:
        """Perform the agent's single responsibility and return validated output."""

    def _input_snapshot(self, context: ProjectContext) -> dict[str, Any]:
        return project_context_payload(context, include_transcript=(self.name == "discovery"))
