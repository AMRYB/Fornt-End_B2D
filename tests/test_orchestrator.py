"""Orchestrator tests: order, dependencies, retries, bounded review, limits."""

from __future__ import annotations

import json

from agentic_core.llm import LLMProviderError
from agentic_core.schemas import ReviewOutput
from tests.helpers import build_handler, detect_agent, review_output_targets


async def test_execution_order(provider, make_orchestrator, make_context):
    provider.set_handler(build_handler())
    orchestrator = make_orchestrator()
    context = make_context("Food delivery.")
    context.status = "ready_for_confirmation"
    orchestrator.confirm(context)

    results = await orchestrator.generate(context)

    order = [detect_agent(call[0]) for call in provider.calls]
    assert order == [
        "requirements",
        "architecture",
        "database",
        "api",
        "devops",
        "reviewer",
    ]
    assert context.status == "approved"
    assert "review" in results


async def test_requirements_artifact_feeds_next_agent(provider, make_orchestrator, make_context):
    provider.set_handler(build_handler())
    orchestrator = make_orchestrator()
    context = make_context("Food delivery.")
    context.status = "ready_for_confirmation"
    orchestrator.confirm(context)

    await orchestrator.generate(context)

    # The architecture agent's prompt must contain the generated requirements.
    architecture_call = next(c for c in provider.calls if detect_agent(c[0]) == "architecture")
    assert "functional_requirements" in architecture_call[1]


async def test_single_review_round_regenerates_once_then_completes(
    provider, make_orchestrator, make_context
):
    """The reviewer runs exactly ONCE. Blocking issues trigger a single
    targeted regeneration pass, then the workflow completes WITHOUT a second
    review (no review -> regenerate -> review -> ... loop)."""
    provider.set_handler(
        build_handler(review_sequence=["needs_revision"])
    )
    orchestrator = make_orchestrator()
    context = make_context("Food delivery.")
    context.status = "ready_for_confirmation"
    orchestrator.confirm(context)

    await orchestrator.generate(context)

    order = [detect_agent(call[0]) for call in provider.calls]
    assert order == [
        "requirements",
        "architecture",
        "database",
        "api",
        "devops",
        "reviewer",    # the one and only review
        "database",    # regenerated + its DevOps dependent
        "devops",
    ]
    reviewer_calls = [c for c in provider.calls if detect_agent(c[0]) == "reviewer"]
    assert len(reviewer_calls) == 1
    assert context.status == "revised"


async def test_regeneration_is_targeted(provider, make_orchestrator, make_context):
    """When the reviewer flags api + devops, ONLY those two are regenerated."""
    base = build_handler(review_sequence=["needs_revision"])

    def handler(system_prompt, user_prompt):
        if detect_agent(system_prompt) == "reviewer":
            return json.dumps(review_output_targets(["api", "devops"]))
        return base(system_prompt, user_prompt)

    provider.set_handler(handler)
    orchestrator = make_orchestrator()
    context = make_context("Food delivery.")
    context.status = "ready_for_confirmation"
    orchestrator.confirm(context)

    await orchestrator.generate(context)

    order = [detect_agent(call[0]) for call in provider.calls]
    assert order == [
        "requirements",
        "architecture",
        "database",
        "api",
        "devops",
        "reviewer",
        "api",
        "devops",
    ]
    assert context.status == "revised"


async def test_regeneration_prompt_contains_existing_artifact_and_issues(
    provider, make_orchestrator, make_context
):
    """A revision is targeted: the agent sees its existing artifact plus the
    exact reviewer issues, not a from-scratch generation."""
    provider.set_handler(build_handler(review_sequence=["needs_revision"]))
    orchestrator = make_orchestrator()
    context = make_context("Food delivery.")
    context.status = "ready_for_confirmation"
    orchestrator.confirm(context)

    await orchestrator.generate(context)

    # Database was flagged by the default needs_revision review.
    regen_calls = [
        c for c in provider.calls
        if detect_agent(c[0]) == "database" and "REVISION TASK" in c[1]
    ]
    assert len(regen_calls) == 1
    assert "preserve everything valid" in regen_calls[0][1]
    assert "Database technology conflicts" in regen_calls[0][1]


async def test_approved_review_completes_without_regeneration(
    provider, make_orchestrator, make_context
):
    provider.set_handler(build_handler(review_status="approved"))
    orchestrator = make_orchestrator()
    context = make_context("Food delivery.")
    context.status = "ready_for_confirmation"
    orchestrator.confirm(context)

    await orchestrator.generate(context)

    assert context.status == "approved"
    order = [detect_agent(call[0]) for call in provider.calls]
    assert order[-1] == "reviewer"


async def test_artifact_revision_limit_reports_needs_attention(
    provider, make_orchestrator, make_context
):
    """With max_artifact_revisions=0 no regeneration is allowed; the workflow
    completes with needs_attention instead of looping."""
    provider.set_handler(build_handler(review_sequence=["needs_revision"]))
    orchestrator = make_orchestrator(max_artifact_revisions=0)
    context = make_context("Food delivery.")
    context.status = "ready_for_confirmation"
    orchestrator.confirm(context)

    await orchestrator.generate(context)

    assert context.status == "needs_attention"
    reviewer_calls = [c for c in provider.calls if detect_agent(c[0]) == "reviewer"]
    assert len(reviewer_calls) == 1
    # No regeneration happened.
    assert [detect_agent(c[0]) for c in provider.calls].count("database") == 1


async def test_failed_regeneration_preserves_previous_artifact(
    provider, make_orchestrator, make_context
):
    """A transient provider failure during regeneration must NOT destroy the
    previously successful artifact: the workflow completes with needs_attention
    and the old artifact is preserved."""
    attempts = {"devops": 0}

    def handler(system_prompt, user_prompt):
        agent = detect_agent(system_prompt)
        if agent == "reviewer":
            return json.dumps(review_output_targets(["api", "devops"]))
        if agent == "devops" and "REVISION TASK" in user_prompt:
            attempts["devops"] += 1
            raise LLMProviderError("Cursor API poll failed: transient 503")
        return build_handler()(system_prompt, user_prompt)

    provider.set_handler(handler)
    orchestrator = make_orchestrator()
    context = make_context("Food delivery.")
    context.status = "ready_for_confirmation"
    orchestrator.confirm(context)

    await orchestrator.generate(context)

    # The regeneration failed (initial + 1 bounded retry), but the first
    # devops artifact is still in place.
    assert context.status == "needs_attention"
    assert context.devops is not None
    assert "dockerfile" in context.devops
    assert attempts["devops"] == 2


async def test_reviewer_prompt_is_compact(provider, make_orchestrator, make_context):
    """The reviewer receives only compact artifact digests — no project
    context, no discovery transcript, no previous review output."""
    provider.set_handler(build_handler(review_status="approved"))
    orchestrator = make_orchestrator()
    context = make_context("Food delivery.")
    context.status = "ready_for_confirmation"
    orchestrator.confirm(context)

    await orchestrator.generate(context)

    review_call = next(c for c in provider.calls if detect_agent(c[0]) == "reviewer")
    user_prompt = review_call[1]
    assert "REQUIREMENTS" in user_prompt
    assert "ARCHITECTURE" in user_prompt
    assert "DATABASE" in user_prompt
    assert "API" in user_prompt
    assert "DEVOPS" in user_prompt
    # No full project context or business idea is forwarded to the reviewer.
    assert "PROJECT CONTEXT" not in user_prompt
    assert "Food delivery." not in user_prompt


async def test_agent_failure_stops_workflow(provider, make_orchestrator, make_context):
    def handler(system_prompt, _user):
        agent = detect_agent(system_prompt)
        if agent == "requirements":
            return "not valid json ever"
        return build_handler()(system_prompt, _user)

    provider.set_handler(handler)
    orchestrator = make_orchestrator()
    context = make_context("Food delivery.")
    context.status = "ready_for_confirmation"
    orchestrator.confirm(context)

    await orchestrator.generate(context)

    assert context.status == "needs_attention"
    # A structured-output failure already exhausted the internal repair retry
    # (initial + 1 repair), so it is NOT retried at the orchestrator level — a
    # full re-run would just double cost. No downstream agent ever executed.
    order = [detect_agent(call[0]) for call in provider.calls]
    assert set(order) == {"requirements"}
    assert len(order) == 2


async def test_provider_failure_is_retried(provider, make_orchestrator, make_context):
    calls = {"requirements": 0}

    def handler(system_prompt, user_prompt):
        agent = detect_agent(system_prompt)
        if agent == "requirements":
            calls["requirements"] += 1
            if calls["requirements"] == 1:
                raise LLMProviderError("transient 502")
        return build_handler()(system_prompt, user_prompt)

    provider.set_handler(handler)
    orchestrator = make_orchestrator()
    context = make_context("Food delivery.")
    context.status = "ready_for_confirmation"
    orchestrator.confirm(context)

    results = await orchestrator.generate(context)

    # Transport-level failures are retryable: the agent is re-run and succeeds.
    assert context.status == "approved"
    assert calls["requirements"] == 2
    assert results["requirements"].status == "success"


async def test_regeneration_targets(make_orchestrator, make_context):
    orchestrator = make_orchestrator()
    assert orchestrator._regeneration_targets(["database"]) == ["database", "devops"]
    assert orchestrator._regeneration_targets(["architecture"]) == [
        "architecture", "database", "api", "devops",
    ]
    assert orchestrator._regeneration_targets(["api"]) == ["api", "devops"]
    assert orchestrator._regeneration_targets(["devops"]) == ["devops"]
    assert orchestrator._regeneration_targets(["requirements"]) == [
        "requirements", "architecture", "database", "api", "devops",
    ]
    assert orchestrator._regeneration_targets([]) == []


async def test_blocking_targets_only(provider, make_orchestrator, make_context):
    """warning/suggestion issues never trigger regeneration; only blocking."""
    orchestrator = make_orchestrator()
    review = ReviewOutput(
        status="needs_revision",
        score=0.6,
        issues=[
            {
                "artifact": "api",
                "severity": "blocking",
                "problem": "real contradiction",
                "expected": "x",
                "actual": "y",
                "fix": "z",
            },
            {
                "artifact": "devops",
                "severity": "warning",
                "problem": "not blocking",
                "expected": "x",
                "actual": "y",
                "fix": "z",
            },
            {
                "artifact": "database",
                "severity": "suggestion",
                "problem": "optional",
                "expected": "x",
                "actual": "y",
                "fix": "z",
            },
        ],
    )
    assert orchestrator._blocking_targets(review) == ["api"]


async def test_generation_requires_confirmation(provider, make_orchestrator, make_context):
    provider.set_handler(build_handler())
    orchestrator = make_orchestrator()
    context = make_context("Food delivery.")

    import pytest

    from agentic_core.orchestrator import OrchestrationError

    with pytest.raises(OrchestrationError):
        await orchestrator.generate(context)


async def test_events_emitted_for_every_step(provider, make_orchestrator, make_context, event_bus):
    provider.set_handler(build_handler())
    orchestrator = make_orchestrator()
    context = make_context("Food delivery.")
    context.status = "ready_for_confirmation"
    orchestrator.confirm(context)

    await orchestrator.generate(context)

    events = [event.event for event in event_bus._buffers.get(context.project_id, [])]
    assert "workflow_started" in events
    assert "agent_started" in events
    assert "agent_completed" in events
    assert "review_started" in events
    assert "review_completed" in events
    assert "workflow_completed" in events
    # Completed events carry token-cost metrics for the CLI/API surface.
    completed = [
        e for e in event_bus._buffers.get(context.project_id, [])
        if e.event == "agent_completed"
    ]
    assert completed
    assert all(e.input_chars > 0 and e.output_chars > 0 for e in completed)


async def test_tracker_records_runs(provider, make_orchestrator, make_context, tracker):
    provider.set_handler(build_handler())
    orchestrator = make_orchestrator()
    context = make_context("Food delivery.")
    context.status = "ready_for_confirmation"
    orchestrator.confirm(context)

    await orchestrator.generate(context)

    records = tracker.list(context.project_id)
    agents = {record.agent for record in records if record.status == "success"}
    assert {"requirements", "architecture", "database", "api", "devops", "reviewer"} <= agents
    assert all(record.duration_ms is not None for record in records if record.status == "success")
    successful = [r for r in records if r.status == "success"]
    assert all(r.input_chars > 0 and r.output_chars > 0 for r in successful)
    # Telemetry recorded per run.
    assert all(r.input_tokens > 0 and r.output_tokens > 0 for r in successful)


async def test_call_counts_reported(provider, make_orchestrator, make_context):
    """The orchestrator returns per-agent invocation counts so the CLI can
    report Total LLM calls (never silently double-calling an agent)."""
    provider.set_handler(build_handler(review_sequence=["needs_revision"]))
    orchestrator = make_orchestrator()
    context = make_context("Food delivery.")
    context.status = "ready_for_confirmation"
    orchestrator.confirm(context)

    results = await orchestrator.generate(context)

    counts = results["call_counts"]
    assert counts == {
        "requirements": 1,
        "architecture": 1,
        "database": 2,   # initial + revision (review flagged database)
        "api": 1,        # generated independently from database
        "devops": 2,     # dependent of database
        "reviewer": 1,   # exactly one review round
    }
    assert results["revisions"] == {
        "requirements": 0,
        "architecture": 0,
        "database": 1,
        "api": 0,
        "devops": 1,
    }