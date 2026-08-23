"""Workflow orchestrator.

Owns the workflow: discovery loop, confirmation gate, dependency-ordered
engineering execution, review, and bounded targeted regeneration. Emits
progress events and tracks every run. Agents never see this workflow logic.

Convergence guarantees (see config):
- ``MAX_REVIEW_ROUNDS``: the reviewer runs at most once per workflow. If it
  finds blocking issues, affected artifacts are regenerated exactly once and the
  workflow completes — never re-reviews.
- ``MAX_ARTIFACT_REVISIONS``: each artifact is regenerated at most once.
- ``MAX_LLM_RETRIES``: transient provider failures are retried a bounded number
  of times; a failed regeneration never overwrites the previous artifact.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import Mapping

from ..agents import RevisionInstruction, build_agents
from ..config import Settings, get_settings
from ..llm import LLMService
from ..schemas import DiscoveryOutput, ProjectContext, ReviewOutput
from .events import AgentEvent, EventBus
from .tracker import ExecutionTracker

ENGINEERING_ORDER = ["requirements", "architecture", "database", "api", "devops"]
ENGINEERING_BATCHES = [
    ["requirements"],
    ["architecture"],
    ["database", "api"],
    ["devops"],
]

SERVERLESS_STAGES: list[tuple[str, list[str]]] = [
    ("requirements", ["requirements"]),
    ("architecture", ["architecture"]),
    ("database_api", ["database", "api"]),
    ("devops", ["devops"]),
    ("review", ["reviewer"]),
]
TERMINAL_PROJECT_STATUSES = {"approved", "revised", "needs_attention"}

# Downstream dependents: regenerating an artifact must also regenerate the
# artifacts that were built on top of it (dependency graph respected).
DEPENDENTS: dict[str, list[str]] = {
    "requirements": ["requirements", "architecture", "database", "api", "devops"],
    "architecture": ["architecture", "database", "api", "devops"],
    "database": ["database", "devops"],
    "api": ["api", "devops"],
    "devops": ["devops"],
}


def execution_batches(names: list[str]) -> list[list[str]]:
    """Return dependency-safe parallel batches for the selected artifacts."""
    selected = set(names)
    return [
        [name for name in batch if name in selected]
        for batch in ENGINEERING_BATCHES
        if any(name in selected for name in batch)
    ]


class OrchestrationError(Exception):
    """Invalid workflow state transition."""


class DiscoveryError(Exception):
    """Discovery agent failed to produce a valid response."""


class Orchestrator:
    def __init__(
        self,
        llm_services: LLMService | Mapping[str, LLMService],
        event_bus: EventBus | None = None,
        tracker: ExecutionTracker | None = None,
        settings: Settings | None = None,
    ):
        self._settings = settings or get_settings()
        self._agents = build_agents(llm_services, tracker)
        self._event_bus = event_bus
        self._tracker = tracker

    # ------------------------------------------------------------------ discovery

    async def discovery_turn(
        self, context: ProjectContext, user_message: str | None = None
    ) -> DiscoveryOutput:
        """Run one discovery step (optionally after a user message)."""
        if context.status not in {"discovery", "ready_for_confirmation"}:
            raise DiscoveryError(
                "Discovery is closed for a project in "
                f"{context.status!r} status."
            )
        if user_message is not None and user_message.strip():
            context.add_turn("user", user_message.strip())

        self._emit(context, "agent_started", agent="discovery")
        result = await self._agents["discovery"].run(context)
        if result.status == "failed":
            # One retry: transient parse/provider hiccups should not crash a
            # conversation that may already have cost the user several calls.
            result = await self._agents["discovery"].run(context)
        if result.status == "failed":
            self._emit(context, "agent_failed", agent="discovery", reason=result.error)
            raise DiscoveryError(result.error or "Discovery agent failed")
        output: DiscoveryOutput = result.output_model

        self._agents["discovery"].apply(context, output)
        context.add_turn("agent", _discovery_message(output))

        context.status = "ready_for_confirmation" if output.status == "ready" else "discovery"
        context.touch()
        self._emit(
            context, "agent_completed", agent="discovery", status="success",
            duration_ms=result.duration_ms,
            input_chars=result.input_chars, output_chars=result.output_chars,
        )
        return output

    def confirm(self, context: ProjectContext) -> ProjectContext:
        """Confirm the discovered project understanding; opens the engineering phase."""
        # Confirmation is a state transition, so replaying the same successful
        # operation is safe and must not fail merely because an HTTP response
        # was lost after the first commit.
        if context.status == "confirmed":
            return context
        if context.status != "ready_for_confirmation":
            raise OrchestrationError(
                "Project is not ready for confirmation (status="
                f"{context.status!r}); discovery must reach 'ready' first."
            )
        context.status = "confirmed"
        context.touch()
        return context

    # ------------------------------------------------------- serverless workflow

    def initialize_generation(self, context: ProjectContext) -> dict[str, object]:
        """Create a durable, resumable generation checkpoint.

        The original :meth:`generate` method remains the best fit for the CLI
        and a long-lived server.  Vercel advances this checkpoint with
        :meth:`generate_next`, keeping each request below the platform's
        function-duration ceiling and persisting state between invocations.
        """

        if context.status == "generating" and context.generation_state:
            return self.generation_snapshot(context)
        if context.status != "confirmed":
            raise OrchestrationError(
                "Generation requires a confirmed project (status="
                f"{context.status!r})."
            )

        # Discovery/create request receipts are deliberately carried into the
        # staged state. If an HTTP response was lost just before confirmation,
        # a browser retry can still prove that the paid operation completed
        # instead of starting it again.
        completed_operations = list(
            context.generation_state.get("completed_operations") or []
        )
        context.status = "generating"
        context.generation_state = {
            "mode": "staged",
            "phase": "initial",
            "next_stage": SERVERLESS_STAGES[0][0],
            "completed_agents": [],
            "pending_revision_batches": [],
            "unresolved": [],
            "call_counts": {
                **{name: 0 for name in ENGINEERING_ORDER},
                "reviewer": 0,
            },
            "revisions": {name: 0 for name in ENGINEERING_ORDER},
            "last_error": None,
        }
        if completed_operations:
            context.generation_state["completed_operations"] = completed_operations[-64:]
        context.touch()
        self._emit(context, "workflow_started")
        return self.generation_snapshot(context)

    def generation_snapshot(self, context: ProjectContext) -> dict[str, object]:
        state = context.generation_state or {}
        completed = list(state.get("completed_agents") or [])
        return {
            "status": context.status,
            "phase": state.get("phase"),
            "next_stage": state.get("next_stage"),
            "completed_agents": completed,
            "call_counts": dict(state.get("call_counts") or {}),
            "revisions": dict(state.get("revisions") or {}),
            "unresolved": list(state.get("unresolved") or []),
            "last_error": state.get("last_error"),
            "complete": context.status in TERMINAL_PROJECT_STATUSES,
        }

    async def generate_next(self, context: ProjectContext) -> dict[str, object]:
        """Run exactly one dependency-safe workflow stage and checkpoint it.

        A stage is one agent, the parallel Database/API pair, the reviewer, or
        one dependency-safe revision batch.  No background task or in-memory
        task registry is required, making the API safe across Vercel instances.
        """

        if context.status == "confirmed":
            self.initialize_generation(context)
        if context.status in TERMINAL_PROJECT_STATUSES:
            return self.generation_snapshot(context)
        if context.status != "generating":
            raise OrchestrationError(
                f"Project cannot advance generation from status={context.status!r}."
            )

        state = context.generation_state
        stage = str(state.get("next_stage") or "")
        if not stage:
            raise OrchestrationError("Generation checkpoint has no next stage")

        call_counts = {
            **{name: 0 for name in ENGINEERING_ORDER},
            "reviewer": 0,
            **dict(state.get("call_counts") or {}),
        }
        revisions = {
            **{name: 0 for name in ENGINEERING_ORDER},
            **dict(state.get("revisions") or {}),
        }
        max_retries = self._settings.max_llm_retries
        completed_now: list[str] = []

        if stage == "review":
            review = await self._run_reviewer(context, call_counts, max_retries)
            if review.status == "failed" or review.output_model is None:
                context.status = "needs_attention"
                state["phase"] = "complete"
                state["next_stage"] = None
                state["last_error"] = "review agent failed repeatedly"
                self._emit(
                    context,
                    "workflow_failed",
                    reason="review agent failed repeatedly",
                )
            elif review.output_model.status == "approved":
                context.status = "approved"
                state["phase"] = "complete"
                state["next_stage"] = None
                completed_now.append("reviewer")
                self._emit(
                    context,
                    "workflow_completed",
                    status="approved",
                    message=(
                        "Blueprint approved "
                        f"(score {review.output_model.score:.2f})"
                    ),
                )
            else:
                completed_now.append("reviewer")
                targets = self._regeneration_targets(
                    self._blocking_targets(review.output_model)
                )
                batches = execution_batches(targets)
                state["pending_revision_batches"] = batches
                state["phase"] = "revision"
                if batches:
                    state["next_stage"] = "revise:" + "+".join(batches[0])
                    self._emit(
                        context,
                        "review_failed",
                        agent="reviewer",
                        reason=f"regenerating: {', '.join(targets)}",
                    )
                else:
                    context.status = "needs_attention"
                    state["phase"] = "complete"
                    state["next_stage"] = None
                    state["last_error"] = (
                        "Review requested revision but named no blocking artifact"
                    )
                    self._emit(
                        context,
                        "workflow_completed",
                        status="needs_attention",
                        reason=str(state["last_error"]),
                    )

        elif stage.startswith("revise:"):
            pending = [
                list(batch) for batch in state.get("pending_revision_batches") or []
            ]
            if not pending:
                raise OrchestrationError("Revision checkpoint has no pending batch")
            batch = pending[0]
            review_model = ReviewOutput.model_validate(context.review or {})
            runnable: list[str] = []
            instructions: dict[str, RevisionInstruction] = {}
            invocations: dict[str, int] = {}
            previous_by_name: dict[str, dict] = {}
            unresolved = list(state.get("unresolved") or [])

            for name in batch:
                if revisions[name] >= self._settings.max_artifact_revisions:
                    unresolved.append(name)
                    self._emit(
                        context,
                        "agent_failed",
                        agent=name,
                        reason=(
                            "revision limit reached "
                            f"({self._settings.max_artifact_revisions})"
                        ),
                    )
                    continue
                previous = dict(getattr(context, name) or {})
                previous_by_name[name] = previous
                instructions[name] = RevisionInstruction(
                    artifact=name,
                    existing=previous,
                    issues=self._issues_for(review_model, name),
                )
                call_counts[name] += 1
                invocations[name] = call_counts[name]
                runnable.append(name)

            batch_results = await asyncio.gather(
                *[
                    self._run_with_retry(
                        context,
                        name,
                        revision=instructions[name],
                        invocation=invocations[name],
                        max_retries=max_retries,
                        reason="review revision",
                        commit_on_success=False,
                    )
                    for name in runnable
                ]
            )
            for name, result in zip(runnable, batch_results):
                revisions[name] += 1
                if result.status == "failed":
                    unresolved.append(name)
                    continue
                self._commit(
                    context,
                    name,
                    result,
                    invocations[name],
                    "review revision",
                )
                completed_now.append(name)
                if self._artifact_hash(previous_by_name[name]) == self._artifact_hash(
                    result.output
                ):
                    unresolved.append(name)
                    self._emit(
                        context,
                        "agent_failed",
                        agent=name,
                        reason=(
                            "regeneration produced no meaningful change "
                            "(hash unchanged)"
                        ),
                    )

            state["unresolved"] = list(dict.fromkeys(unresolved))
            pending.pop(0)
            state["pending_revision_batches"] = pending
            if pending:
                state["next_stage"] = "revise:" + "+".join(pending[0])
            elif state["unresolved"]:
                context.status = "needs_attention"
                state["phase"] = "complete"
                state["next_stage"] = None
                self._emit(
                    context,
                    "workflow_completed",
                    status="needs_attention",
                    reason=(
                        "Blocking issues remain for: "
                        + ", ".join(sorted(state["unresolved"]))
                    ),
                )
            else:
                context.status = "revised"
                state["phase"] = "complete"
                state["next_stage"] = None
                self._emit(
                    context,
                    "workflow_completed",
                    status="revised",
                    message=(
                        "Artifacts revised once to address review findings "
                        "(no second review by design)"
                    ),
                )

        else:
            stage_map = dict(SERVERLESS_STAGES)
            batch = stage_map.get(stage)
            if not batch or batch == ["reviewer"]:
                raise OrchestrationError(f"Unknown generation stage: {stage!r}")
            invocations: dict[str, int] = {}
            for name in batch:
                call_counts[name] += 1
                invocations[name] = call_counts[name]
            batch_results = await asyncio.gather(
                *[
                    self._run_with_retry(
                        context,
                        name,
                        invocation=invocations[name],
                        max_retries=max_retries,
                        commit_on_success=False,
                    )
                    for name in batch
                ]
            )
            failed: list[str] = []
            for name, result in zip(batch, batch_results):
                if result.status == "success":
                    self._commit(context, name, result, invocations[name], None)
                    completed_now.append(name)
                else:
                    failed.append(name)
            if failed:
                context.status = "needs_attention"
                state["phase"] = "complete"
                state["next_stage"] = None
                state["last_error"] = (
                    f"{', '.join(failed)} generation failed repeatedly"
                )
                state["unresolved"] = list(
                    dict.fromkeys(list(state.get("unresolved") or []) + failed)
                )
                self._emit(
                    context,
                    "workflow_failed",
                    reason=str(state["last_error"]),
                )
            else:
                stage_names = [name for name, _batch in SERVERLESS_STAGES]
                next_index = stage_names.index(stage) + 1
                state["next_stage"] = stage_names[next_index]

        completed = list(state.get("completed_agents") or [])
        for name in completed_now:
            if name not in completed:
                completed.append(name)
        state["completed_agents"] = completed
        state["call_counts"] = call_counts
        state["revisions"] = revisions
        state["last_stage"] = stage
        context.touch()

        snapshot = self.generation_snapshot(context)
        snapshot["stage"] = stage
        snapshot["completed_now"] = completed_now
        return snapshot

    # ----------------------------------------------------------------- engineering

    async def generate(self, context: ProjectContext) -> dict[str, object]:
        """Run the full engineering workflow: requirements -> ... -> review.

        Bounded and convergent:
          1. Generate all artifacts (dependency-ordered).
          2. Review exactly once.
          3. If blocking issues: targeted regeneration of affected artifacts
             (max one revision each), then complete — NO second review.

        Per-workflow state (revision counters, call counts) is local to this
        call so no state leaks between projects.
        """
        if context.status != "confirmed":
            raise OrchestrationError(
                "Generation requires a confirmed project (status="
                f"{context.status!r})."
            )
        context.status = "generating"
        self._emit(context, "workflow_started")
        results: dict[str, object] = {}
        revisions: dict[str, int] = {name: 0 for name in ENGINEERING_ORDER}
        call_counts: dict[str, int] = {name: 0 for name in ENGINEERING_ORDER}
        call_counts["reviewer"] = 0
        max_artifact_revisions = self._settings.max_artifact_revisions
        max_llm_retries = self._settings.max_llm_retries

        # Phase 1: requirements -> architecture -> (database || api) -> devops.
        # Parallel batch results are committed in stable order after every task
        # finishes, so downstream agents always see a complete upstream stage.
        for batch in execution_batches(ENGINEERING_ORDER):
            invocations = {}
            for name in batch:
                call_counts[name] += 1
                invocations[name] = call_counts[name]
            batch_results = await asyncio.gather(
                *[
                    self._run_with_retry(
                        context,
                        name,
                        invocation=invocations[name],
                        max_retries=max_llm_retries,
                        commit_on_success=False,
                    )
                    for name in batch
                ]
            )
            failed: list[str] = []
            for name, result in zip(batch, batch_results):
                results[name] = result
                if result.status == "success":
                    self._commit(context, name, result, invocations[name], None)
                else:
                    failed.append(name)
            if failed:
                context.status = "needs_attention"
                self._emit(
                    context, "workflow_failed",
                    reason=f"{', '.join(failed)} generation failed repeatedly",
                )
                return self._summary(results, call_counts, revisions)

        # Phase 2: exactly one review round.
        review = await self._run_reviewer(context, call_counts, max_llm_retries)
        results["review"] = review
        if review.status == "failed":
            context.status = "needs_attention"
            self._emit(
                context, "workflow_failed",
                reason="review agent failed repeatedly",
            )
            return self._summary(results, call_counts, revisions)

        if review.output_model.status == "approved":
            context.status = "approved"
            self._emit(
                context, "workflow_completed", status="approved",
                message=f"Blueprint approved (score {review.output_model.score:.2f})",
            )
            return self._summary(results, call_counts, revisions)

        # Phase 3: targeted regeneration — bounded, dependency-ordered, with a
        # hard per-artifact revision cap. No second review by design.
        blocking_targets = self._blocking_targets(review.output_model)
        targets = self._regeneration_targets(blocking_targets)
        if targets:
            self._emit(
                context, "review_failed", agent="reviewer",
                reason=f"regenerating: {', '.join(targets)}",
            )

        unresolved: list[str] = []
        for batch in execution_batches(targets):
            runnable: list[str] = []
            previous_by_name: dict[str, dict] = {}
            invocations: dict[str, int] = {}
            revisions_by_name: dict[str, RevisionInstruction] = {}
            for name in batch:
                if revisions[name] >= max_artifact_revisions:
                    unresolved.append(name)
                    self._emit(
                        context, "agent_failed", agent=name,
                        reason=f"revision limit reached ({max_artifact_revisions})",
                    )
                    continue
                previous = dict(getattr(context, name) or {})
                previous_by_name[name] = previous
                revisions_by_name[name] = RevisionInstruction(
                    artifact=name,
                    existing=previous,
                    issues=self._issues_for(review.output_model, name),
                )
                call_counts[name] += 1
                invocations[name] = call_counts[name]
                runnable.append(name)

            batch_results = await asyncio.gather(
                *[
                    self._run_with_retry(
                        context,
                        name,
                        revision=revisions_by_name[name],
                        invocation=invocations[name],
                        max_retries=max_llm_retries,
                        reason="review revision",
                        commit_on_success=False,
                    )
                    for name in runnable
                ]
            )
            for name, result in zip(runnable, batch_results):
                revisions[name] += 1
                results[name] = result
                if result.status == "failed":
                    # Preserve the last successful artifact: only successful
                    # revisions are committed below.
                    unresolved.append(name)
                    continue
                self._commit(
                    context,
                    name,
                    result,
                    invocations[name],
                    "review revision",
                )
                if self._artifact_hash(previous_by_name[name]) == self._artifact_hash(
                    result.output
                ):
                    unresolved.append(name)
                    self._emit(
                        context, "agent_failed", agent=name,
                        reason="regeneration produced no meaningful change (hash unchanged)",
                    )

        if unresolved:
            context.status = "needs_attention"
            self._emit(
                context, "workflow_completed", status="needs_attention",
                reason=f"Blocking issues remain for: {', '.join(sorted(set(unresolved)))}",
            )
        else:
            context.status = "revised"
            self._emit(
                context, "workflow_completed", status="revised",
                message=(
                    "Artifacts revised once to address review findings "
                    "(no second review by design)"
                ),
            )
        return self._summary(results, call_counts, revisions)

    async def _run_reviewer(
        self,
        context: ProjectContext,
        call_counts: dict[str, int],
        max_retries: int,
    ):
        self._emit(context, "review_started")
        call_counts["reviewer"] += 1
        review = await self._agents["reviewer"].run(context)
        if review.status == "failed" and review.retryable and max_retries > 0:
            self._emit(context, "agent_retrying", agent="reviewer", reason=review.error)
            call_counts["reviewer"] += 1
            review = await self._agents["reviewer"].run(context)
        context.review = review.output
        self._emit(
            context, "review_completed", agent="reviewer", status=review.status,
            duration_ms=review.duration_ms,
            input_chars=review.input_chars, output_chars=review.output_chars,
        )
        return review

    # ---------------------------------------------------------------- internals

    async def _run_with_retry(
        self,
        context: ProjectContext,
        name: str,
        *,
        revision: RevisionInstruction | None = None,
        invocation: int = 1,
        max_retries: int = 1,
        reason: str | None = None,
        commit_on_success: bool = True,
    ):
        reason = reason or ("review revision" if revision is not None else None)
        self._emit(
            context, "agent_started", agent=name,
            invocation=invocation, reason=reason,
        )
        result = await self._agents[name].run(context, revision=revision)
        if result.status == "success":
            if commit_on_success:
                self._commit(context, name, result, invocation, reason)
            return result

        if not result.retryable or max_retries <= 0:
            # Structured-output failures already consumed internal repair
            # retries; a full re-run only doubles cost, so stop here.
            self._emit(
                context, "agent_failed", agent=name,
                reason=result.error, invocation=invocation,
            )
            return result

        self._emit(
            context, "agent_retrying", agent=name,
            reason=result.error, invocation=invocation,
        )
        result = await self._agents[name].run(context, revision=revision)
        if result.status == "success":
            if commit_on_success:
                self._commit(context, name, result, invocation, reason)
            return result

        self._emit(
            context, "agent_failed", agent=name,
            reason=result.error, invocation=invocation,
        )
        return result

    def _commit(self, context, name, result, invocation, reason) -> None:
        """Replace the artifact in context only on success (never overwrite a
        good artifact with a failed regeneration)."""
        setattr(context, name, result.output)
        self._emit(
            context, "agent_completed", agent=name, status="success",
            duration_ms=result.duration_ms,
            input_chars=result.input_chars, output_chars=result.output_chars,
            invocation=invocation, reason=reason,
        )

    def _blocking_targets(self, review: ReviewOutput) -> list[str]:
        """Artifacts that must be regenerated, from blocking issues only.

        ``warning``/``suggestion`` issues never trigger regeneration, and only
        artifacts the reviewer explicitly flagged are regenerated.
        """
        targets: list[str] = []
        for issue in review.issues:
            if issue.severity == "blocking" and issue.artifact in ENGINEERING_ORDER:
                if issue.artifact not in targets:
                    targets.append(issue.artifact)
        for artifact in review.artifacts_to_regenerate:
            if artifact in ENGINEERING_ORDER and artifact not in targets:
                targets.append(artifact)
        return targets

    def _issues_for(self, review: ReviewOutput, name: str) -> list[dict]:
        issues = [i.model_dump() for i in review.issues if i.artifact == name]
        if not issues:
            issues = [i.model_dump() for i in review.issues if i.severity == "blocking"]
        return issues

    def _regeneration_targets(self, artifacts: list[str]) -> list[str]:
        """Expand targets through the dependency graph, topologically ordered."""
        expanded: list[str] = []
        for artifact in artifacts:
            for dependent in DEPENDENTS.get(artifact, [artifact]):
                if dependent not in expanded:
                    expanded.append(dependent)
        return [name for name in ENGINEERING_ORDER if name in expanded]

    @staticmethod
    def _artifact_hash(artifact: dict | None) -> str:
        normalized = json.dumps(artifact or {}, sort_keys=True, default=str)
        return hashlib.sha256(normalized.encode("utf-8")).hexdigest()

    def _summary(
        self,
        results: dict[str, object],
        call_counts: dict[str, int],
        revisions: dict[str, int],
    ) -> dict[str, object]:
        results["call_counts"] = dict(call_counts)
        results["revisions"] = dict(revisions)
        return results

    def _emit(
        self,
        context: ProjectContext,
        event: str,
        agent: str | None = None,
        status: str | None = None,
        duration_ms: int | None = None,
        reason: str | None = None,
        message: str | None = None,
        input_chars: int | None = None,
        output_chars: int | None = None,
        invocation: int | None = None,
    ) -> None:
        if self._event_bus is None:
            return
        self._event_bus.emit(
            AgentEvent(
                event=event,
                project_id=context.project_id,
                agent=agent,
                status=status,
                duration_ms=duration_ms,
                reason=reason,
                message=message,
                input_chars=input_chars,
                output_chars=output_chars,
                invocation=invocation,
            )
        )


def _discovery_message(output: DiscoveryOutput) -> str:
    from ..agents import discovery_agent_message

    return discovery_agent_message(output)
