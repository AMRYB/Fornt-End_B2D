"""Discovery agent / conversation loop tests."""

from __future__ import annotations

import json

import pytest

from agentic_core.orchestrator import DiscoveryError, OrchestrationError
from agentic_core.schemas import DiscoveryOutput, DiscoveryQuestion
from tests.helpers import discovery_output, build_handler


async def test_vague_idea_asks_for_clarification(provider, make_orchestrator, make_context):
    provider.set_handler(build_handler(discovery_status="needs_clarification"))
    orchestrator = make_orchestrator()
    context = make_context("I want to build a booking platform for football fields.")

    output = await orchestrator.discovery_turn(context, context.business_idea)

    assert output.status == "needs_clarification"
    assert output.questions
    assert context.status == "discovery"
    assert len(context.transcript) == 2  # user + agent
    assert context.transcript[0].role == "user"
    assert context.transcript[1].role == "agent"


async def test_complete_idea_reaches_ready(provider, make_orchestrator, make_context):
    provider.set_handler(build_handler(discovery_status="ready"))
    orchestrator = make_orchestrator()
    context = make_context("Food delivery platform with online payment.")

    output = await orchestrator.discovery_turn(context, context.business_idea)

    assert output.status == "ready"
    assert output.questions == []
    assert context.status == "ready_for_confirmation"
    assert context.target_users == ["Customers", "Restaurants"]
    assert context.user_roles == ["Customer", "Restaurant Owner", "Admin"]


async def test_confirmation_requires_ready(provider, make_orchestrator, make_context):
    provider.set_handler(build_handler(discovery_status="needs_clarification"))
    orchestrator = make_orchestrator()
    context = make_context("Something vague.")

    await orchestrator.discovery_turn(context, context.business_idea)
    with pytest.raises(OrchestrationError):
        orchestrator.confirm(context)

    provider.set_handler(build_handler(discovery_status="ready"))
    context2 = make_context("A clear, complete idea.")
    await orchestrator.discovery_turn(context2, context2.business_idea)
    orchestrator.confirm(context2)
    assert context2.status == "confirmed"
    assert orchestrator.confirm(context2) is context2
    assert context2.status == "confirmed"


async def test_agent_never_loses_conversation_history(provider, make_orchestrator, make_context):
    """The discovery prompt must include prior turns so the agent never re-asks."""
    responses = [
        json.dumps(discovery_output("needs_clarification")),
        json.dumps(discovery_output("ready")),
    ]
    provider.set_responses(responses)
    orchestrator = make_orchestrator()
    context = make_context("Booking platform.")

    await orchestrator.discovery_turn(context, context.business_idea)
    await orchestrator.discovery_turn(context, "Players, field owners and admins.")

    second_call_user = provider.calls[1][1]
    assert "Booking platform." in second_call_user
    assert "Players, field owners and admins." in second_call_user
    assert context.transcript[-1].role == "agent"
    assert context.status == "ready_for_confirmation"


async def test_contradictory_information_last_answer_wins(provider, make_orchestrator, make_context):
    provider.set_handler(
        lambda _s, _u: json.dumps(
            discovery_output("needs_clarification", ) | {"known_information": {"target_users": ["Players"]}}
        )
    )
    orchestrator = make_orchestrator()
    context = make_context("Football field booking.")
    await orchestrator.discovery_turn(context, context.business_idea)
    assert context.target_users == ["Players"]

    provider.set_handler(
        lambda _s, _u: json.dumps(
            discovery_output("ready") | {"known_information": {"target_users": ["Owners"]}}
        )
    )
    await orchestrator.discovery_turn(context, "Actually owners only.")
    assert context.target_users == ["Owners"]


async def test_unnecessary_information_not_forced(provider, make_orchestrator, make_context):
    """not_applicable fields produce no questions."""
    payload = discovery_output("needs_clarification")
    payload["missing_information"] = [
        {"field": "payments", "importance": "not_applicable", "reason": "No payments in this project."}
    ]
    payload["questions"] = []
    provider.set_handler(lambda _s, _u: json.dumps(payload))
    orchestrator = make_orchestrator()
    context = make_context("Internal tool, no payments.")

    output = await orchestrator.discovery_turn(context, context.business_idea)

    assert output.status == "needs_clarification"
    assert output.missing_information[0].importance == "not_applicable"


async def test_known_information_application_is_idempotent(provider, make_orchestrator, make_context):
    def handler(_s, _u):
        return json.dumps(
            discovery_output("ready") | {"known_information": {"core_features": ["Booking", "Payments"]}}
        )

    provider.set_handler(handler)
    orchestrator = make_orchestrator()
    context = make_context("Football booking.")
    context.core_features = ["Booking", "Payments"]

    await orchestrator.discovery_turn(context, context.business_idea)

    assert context.core_features == ["Booking", "Payments"]


async def test_discovery_questions_support_options(provider, make_orchestrator, make_context):
    """Questions can carry multiple-choice options; users pick one or type their own."""
    payload = discovery_output("needs_clarification")
    payload["questions"] = [
        {
            "id": "q1",
            "question": "Which client should v1 ship on?",
            "reason": "Defines the build target.",
            "options": ["Web app only", "Mobile app only", "Both web and mobile"],
        }
    ]
    provider.set_handler(lambda _s, _u: json.dumps(payload))
    orchestrator = make_orchestrator()
    context = make_context("Booking platform.")

    output = await orchestrator.discovery_turn(context, context.business_idea)

    question = output.questions[0]
    assert isinstance(question, DiscoveryQuestion)
    assert question.options == ["Web app only", "Mobile app only", "Both web and mobile"]
    assert output.status == "needs_clarification"


async def test_discovery_failure_raises(provider, make_orchestrator, make_context):
    async def boom(_s, _u):
        raise RuntimeError("provider down")

    provider.set_handler(boom)
    orchestrator = make_orchestrator()
    context = make_context("Football booking.")

    with pytest.raises(Exception):
        await orchestrator.discovery_turn(context, context.business_idea)


async def test_discovery_retries_transient_failure(provider, make_orchestrator, make_context):
    """A single unparseable reply must not kill discovery; it retries once."""
    calls = {"n": 0}

    def flaky(_s, _u):
        calls["n"] += 1
        if calls["n"] <= 2:  # first run: generate + repair both fail
            return "sorry, no json here"
        return json.dumps(discovery_output("ready"))

    provider.set_handler(flaky)
    orchestrator = make_orchestrator()
    context = make_context("Football booking.")

    output = await orchestrator.discovery_turn(context, context.business_idea)

    assert calls["n"] == 3  # 2 (failed run) + 1 (successful retry run)
    assert output.status == "ready"
    assert context.status == "ready_for_confirmation"


async def test_discovery_fails_after_two_bad_replies(provider, make_orchestrator, make_context):
    calls = {"n": 0}

    def always_bad(_s, _u):
        calls["n"] += 1
        return "still not json"

    provider.set_handler(always_bad)
    orchestrator = make_orchestrator()
    context = make_context("Football booking.")

    with pytest.raises(Exception):
        await orchestrator.discovery_turn(context, context.business_idea)

    assert calls["n"] == 4  # two runs, each generate + repair


@pytest.mark.parametrize("status", ["confirmed", "generating", "approved", "revised", "needs_attention"])
async def test_discovery_cannot_downgrade_engineering_or_terminal_projects(
    status, provider, make_orchestrator, make_context
):
    orchestrator = make_orchestrator()
    context = make_context("Already advanced project.")
    context.status = status

    with pytest.raises(DiscoveryError, match="Discovery is closed"):
        await orchestrator.discovery_turn(context, "stale message")

    assert provider.calls == []
    assert context.status == status
