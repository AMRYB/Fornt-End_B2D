from __future__ import annotations

import asyncio
import json

from agentic_core.orchestrator import execution_batches
from tests.helpers import build_handler, detect_agent, review_output_targets


def test_execution_batches_respect_dependencies():
    assert execution_batches(
        ["requirements", "architecture", "database", "api", "devops"]
    ) == [
        ["requirements"],
        ["architecture"],
        ["database", "api"],
        ["devops"],
    ]
    assert execution_batches(["architecture", "database", "api", "devops"]) == [
        ["architecture"],
        ["database", "api"],
        ["devops"],
    ]
    assert execution_batches(["database", "devops"]) == [
        ["database"],
        ["devops"],
    ]


async def test_database_and_api_overlap_before_devops(
    provider, make_orchestrator, make_context, event_bus
):
    base = build_handler()
    started: dict[str, float] = {}
    finished: dict[str, float] = {}

    async def handler(system_prompt, user_prompt):
        agent = detect_agent(system_prompt)
        started[agent] = asyncio.get_running_loop().time()
        if agent == "database":
            await asyncio.sleep(0.05)
        elif agent == "api":
            await asyncio.sleep(0.01)
        result = base(system_prompt, user_prompt)
        finished[agent] = asyncio.get_running_loop().time()
        return result

    provider.set_handler(handler)
    orchestrator = make_orchestrator()
    context = make_context("Food delivery.")
    context.status = "ready_for_confirmation"
    orchestrator.confirm(context)

    await orchestrator.generate(context)

    assert started["database"] < finished["api"]
    assert started["api"] < finished["database"]
    assert started["devops"] >= max(finished["database"], finished["api"])
    completed = [
        event.agent
        for event in event_bus._buffers[context.project_id]
        if event.event == "agent_completed"
    ]
    assert completed.index("database") < completed.index("api")
    assert context.status == "approved"


async def test_architecture_revision_fans_out_database_and_api(
    provider, make_orchestrator, make_context
):
    base = build_handler()
    started: dict[str, float] = {}
    finished: dict[str, float] = {}

    async def handler(system_prompt, user_prompt):
        agent = detect_agent(system_prompt)
        if agent == "reviewer":
            return json.dumps(review_output_targets(["architecture"]))
        if "REVISION TASK" in user_prompt:
            started[agent] = asyncio.get_running_loop().time()
            if agent in {"database", "api"}:
                await asyncio.sleep(0.03)
            result = base(system_prompt, user_prompt)
            finished[agent] = asyncio.get_running_loop().time()
            return result
        return base(system_prompt, user_prompt)

    provider.set_handler(handler)
    orchestrator = make_orchestrator()
    context = make_context("Food delivery.")
    context.status = "ready_for_confirmation"
    orchestrator.confirm(context)

    await orchestrator.generate(context)

    assert finished["architecture"] <= min(started["database"], started["api"])
    assert started["database"] < finished["api"]
    assert started["api"] < finished["database"]
    assert started["devops"] >= max(finished["database"], finished["api"])
