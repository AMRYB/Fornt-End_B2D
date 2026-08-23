"""End-to-end test: Online Food Delivery Platform full workflow."""

from __future__ import annotations

from agentic_core.artifacts import render_all
from tests.helpers import build_handler


async def test_full_workflow_end_to_end(provider, make_orchestrator, make_context, artifact_store):
    provider.set_handler(build_handler(discovery_status="ready"))
    orchestrator = make_orchestrator()
    context = make_context("Online food delivery platform where customers order from local restaurants.")

    # 1. Discovery -> ready
    output = await orchestrator.discovery_turn(context, context.business_idea)
    assert output.status == "ready"
    assert context.status == "ready_for_confirmation"
    assert context.target_users == ["Customers", "Restaurants"]

    # 2. Confirmation
    orchestrator.confirm(context)
    assert context.status == "confirmed"

    # 3. Autonomous engineering
    results = await orchestrator.generate(context)
    assert context.status == "approved"

    # 4. Final blueprint artifacts
    files = render_all(context)
    expected = {
        "overview.md",
        "requirements.md",
        "architecture.md",
        "architecture.mmd",
        "database.md",
        "database.sql",
        "erd.mmd",
        "api.md",
        "openapi.yaml",
        "devops.md",
        "Dockerfile",
        "docker-compose.yml",
        "github-actions.yml",
    }
    assert set(files) == expected
    assert "CREATE TABLE" in files["database.sql"]
    assert files["Dockerfile"].startswith("FROM python")

    # 5. Artifacts persisted to the store
    for name in ("overview.md", "database.sql", "openapi.yaml", "Dockerfile"):
        content = artifact_store.read(context.project_id, name)
        assert content is None  # only written by the API generation task, not here

    # store writes are exercised in the API layer; verify store round-trip manually
    artifact_store.write(context.project_id, "requirements.md", files["requirements.md"])
    assert artifact_store.list(context.project_id) == ["requirements.md"]
    assert "Functional" in artifact_store.read(context.project_id, "requirements.md")


async def test_full_conversation_discovery(provider, make_orchestrator, make_context):
    from tests.helpers import discovery_output

    import json

    import pytest

    turns = [
        ("I want to build a platform where users can book football fields.", "needs_clarification"),
        ("Players and field owners, plus admins.", "needs_clarification"),
        ("Players should pay online when booking.", "needs_clarification"),
        ("Yes, field owners manage their own availability and pricing.", "ready"),
    ]

    async def handler(_s, _u):
        answer = turns[len(provider.calls) - 1][1]
        return json.dumps(discovery_output(answer))

    provider.set_handler(handler)
    orchestrator = make_orchestrator()
    context = make_context("placeholder")

    statuses = []
    for message, _ in turns:
        output = await orchestrator.discovery_turn(context, message)
        statuses.append(output.status)

    assert statuses[:3] == ["needs_clarification"] * 3
    assert statuses[-1] == "ready"
    assert context.status == "ready_for_confirmation"
    assert len(context.transcript) == 8  # 4 user + 4 agent turns