"""Checkpointed workflow tests for Vercel's serverless execution model."""

from tests.helpers import build_handler, detect_agent


async def test_staged_generation_completes_across_dependency_safe_requests(
    provider, make_orchestrator, make_context
):
    provider.set_handler(build_handler(review_status="approved"))
    orchestrator = make_orchestrator()
    context = make_context("Food delivery.")
    context.status = "ready_for_confirmation"
    orchestrator.confirm(context)

    initialized = orchestrator.initialize_generation(context)
    assert initialized["next_stage"] == "requirements"
    assert initialized["complete"] is False

    stages = []
    for _ in range(8):
        progress = await orchestrator.generate_next(context)
        stages.append(progress["stage"])
        if progress["complete"]:
            break

    assert stages == [
        "requirements",
        "architecture",
        "database_api",
        "devops",
        "review",
    ]
    assert context.status == "approved"
    assert context.requirements and context.architecture
    assert context.database and context.api and context.devops and context.review
    assert [detect_agent(call[0]) for call in provider.calls] == [
        "requirements",
        "architecture",
        "database",
        "api",
        "devops",
        "reviewer",
    ]


async def test_staged_generation_persists_targeted_revision_batches(
    provider, make_orchestrator, make_context
):
    provider.set_handler(build_handler(review_sequence=["needs_revision"]))
    orchestrator = make_orchestrator()
    context = make_context("Food delivery.")
    context.status = "ready_for_confirmation"
    orchestrator.confirm(context)
    orchestrator.initialize_generation(context)

    for _ in range(10):
        progress = await orchestrator.generate_next(context)
        if progress["complete"]:
            break

    assert context.status == "revised"
    assert context.generation_state["pending_revision_batches"] == []
    assert context.generation_state["revisions"]["database"] == 1
    assert context.generation_state["revisions"]["devops"] == 1
    assert [detect_agent(call[0]) for call in provider.calls] == [
        "requirements",
        "architecture",
        "database",
        "api",
        "devops",
        "reviewer",
        "database",
        "devops",
    ]


async def test_staged_generation_initialize_is_idempotent_while_running(
    provider, make_orchestrator, make_context
):
    provider.set_handler(build_handler())
    orchestrator = make_orchestrator()
    context = make_context("Food delivery.")
    context.status = "ready_for_confirmation"
    orchestrator.confirm(context)

    first = orchestrator.initialize_generation(context)
    second = orchestrator.initialize_generation(context)

    assert first == second
    assert provider.calls == []

