"""Agent-level tests: structured output handling and failure modes."""

from __future__ import annotations

import json

from agentic_core.agents import (
    APIAgent,
    ArchitectureAgent,
    DatabaseAgent,
    DevOpsAgent,
    RequirementsAgent,
    RevisionInstruction,
)
from agentic_core.llm import LLMProviderError
from tests.helpers import (
    api_output,
    architecture_output,
    database_output,
    devops_output,
    requirements_output,
)


async def test_requirements_agent_valid_output(provider, llm_service, make_context):
    provider.set_responses([json.dumps(requirements_output())])
    agent = RequirementsAgent(llm_service)
    result = await agent.run(make_context("Food delivery."))

    assert result.status == "success"
    assert result.output["functional_requirements"]
    assert result.error is None


async def test_malformed_json_recovers_via_repair(provider, llm_service, make_context, settings):
    provider.set_responses(["this is not json at all", json.dumps(requirements_output())])
    agent = RequirementsAgent(llm_service)
    result = await agent.run(make_context("Food delivery."))

    assert result.status == "success"
    assert result.retry_count == 1
    assert len(provider.calls) == 2


async def test_invalid_field_type_repaired(provider, llm_service, make_context):
    broken = requirements_output()
    broken["functional_requirements"] = "not-a-list"
    provider.set_responses([json.dumps(broken), json.dumps(requirements_output())])
    agent = RequirementsAgent(llm_service)
    result = await agent.run(make_context("Food delivery."))

    assert result.status == "success"
    assert result.retry_count == 1


async def test_persistent_invalid_output_fails(provider, llm_service, make_context, settings):
    provider.set_responses(["bad json", "still bad json"])
    agent = RequirementsAgent(llm_service)
    result = await agent.run(make_context("Food delivery."))

    assert result.status == "failed"
    assert result.error is not None
    assert result.output is None


async def test_llm_failure_marks_agent_failed(provider, llm_service, make_context):
    async def boom(_s, _u):
        raise LLMProviderError("upstream 500")

    provider.set_handler(boom)
    agent = RequirementsAgent(llm_service)
    result = await agent.run(make_context("Food delivery."))

    assert result.status == "failed"
    assert "upstream 500" in (result.error or "")
    # Transport-level failures are retryable at the orchestrator level.
    assert result.retryable is True


async def test_database_agent_receives_architecture_input(provider, llm_service, make_context):
    provider.set_responses([json.dumps(database_output())])
    agent = DatabaseAgent(llm_service)
    context = make_context("Food delivery.")
    context.architecture = architecture_output()

    await agent.run(context)

    user_prompt = provider.calls[0][1]
    assert "ARCHITECTURE" in user_prompt
    assert "FastAPI" in user_prompt


async def test_api_agent_uses_requirements_and_architecture(
    provider, llm_service, make_context
):
    agent = APIAgent(llm_service)
    context = make_context("Food delivery.")
    context.requirements = requirements_output()
    context.architecture = architecture_output()
    provider.set_responses([json.dumps(api_output())])

    await agent.run(context)

    user_prompt = provider.calls[0][1]
    assert "REQUIREMENTS SPECIFICATION" in user_prompt
    assert "ARCHITECTURE" in user_prompt
    assert "DATABASE DESIGN" not in user_prompt


async def test_api_prompt_uses_digested_inputs(provider, llm_service, make_context):
    """The API prompt is independent from the concurrently generated database."""
    agent = APIAgent(llm_service)
    context = make_context("Food delivery.")
    context.requirements = requirements_output()
    context.architecture = architecture_output()
    provider.set_responses([json.dumps(api_output())])

    await agent.run(context)

    user_prompt = provider.calls[0][1]
    assert "FastAPI" in user_prompt
    assert "DATABASE DESIGN" not in user_prompt
    assert "sql_schema" not in user_prompt
    assert "erd_mermaid" not in user_prompt
    assert "CREATE TABLE" not in user_prompt


async def test_api_schema_excludes_derived_openapi(provider, llm_service, make_context):
    """The model is never shown openapi_spec in the JSON schema, so it cannot
    spend output tokens producing the (derived) OpenAPI document."""
    agent = APIAgent(llm_service)
    context = make_context("Food delivery.")
    context.requirements = requirements_output()
    context.architecture = architecture_output()
    provider.set_responses([json.dumps(api_output())])

    await agent.run(context)

    assert "openapi_spec" not in provider.calls[0][1]


async def test_database_schema_excludes_derived_sql_and_erd(provider, llm_service, make_context):
    """sql_schema and erd_mermaid are excluded from the schema the model sees;
    the renderer derives them from the entities/fields instead."""
    agent = DatabaseAgent(llm_service)
    context = make_context("Food delivery.")
    context.requirements = requirements_output()
    context.architecture = architecture_output()
    provider.set_responses([json.dumps(database_output())])

    await agent.run(context)

    user_prompt = provider.calls[0][1]
    assert "sql_schema" not in user_prompt
    assert "erd_mermaid" not in user_prompt


async def test_result_reports_token_metrics(provider, llm_service, make_context):
    provider.set_responses([json.dumps(requirements_output())])
    agent = RequirementsAgent(llm_service)
    result = await agent.run(make_context("Food delivery."))

    assert result.status == "success"
    assert result.input_chars > 0
    assert result.output_chars > 0


async def test_telemetry_failure_never_discards_a_valid_model_result(
    provider, llm_service, make_context
):
    class BrokenTracker:
        def start(self, *_args, **_kwargs):
            raise RuntimeError("telemetry unavailable")

    provider.set_responses([json.dumps(requirements_output())])
    agent = RequirementsAgent(llm_service, tracker=BrokenTracker())

    result = await agent.run(make_context("Food delivery."))

    assert result.status == "success"
    assert result.output["functional_requirements"]


async def test_telemetry_completion_failure_is_best_effort(
    provider, llm_service, make_context
):
    class BrokenTracker:
        def start(self, *_args, **_kwargs):
            return object()

        def complete(self, *_args, **_kwargs):
            raise RuntimeError("telemetry unavailable")

    provider.set_responses([json.dumps(requirements_output())])
    agent = RequirementsAgent(llm_service, tracker=BrokenTracker())

    result = await agent.run(make_context("Food delivery."))

    assert result.status == "success"


async def test_structured_failure_not_retryable(provider, llm_service, make_context, settings):
    """Persistent structured-output failure is not orchestrator-retryable: the
    internal repairs already consumed the retry budget."""
    provider.set_responses(["bad json", "still bad json"])
    agent = RequirementsAgent(llm_service)
    result = await agent.run(make_context("Food delivery."))

    assert result.status == "failed"
    assert result.retryable is False


async def test_devops_agent_receives_full_stack(provider, llm_service, make_context):
    agent = DevOpsAgent(llm_service)
    context = make_context("Food delivery.")
    context.requirements = requirements_output()
    context.architecture = architecture_output()
    context.api = api_output()
    provider.set_responses([json.dumps(devops_output())])

    await agent.run(context)

    user_prompt = provider.calls[0][1]
    assert "API DESIGN" in user_prompt
    assert "PostgreSQL" in user_prompt


async def test_architecture_agent_valid_output(provider, llm_service, make_context):
    provider.set_responses([json.dumps(architecture_output())])
    agent = ArchitectureAgent(llm_service)
    context = make_context("Food delivery.")
    context.requirements = requirements_output()

    result = await agent.run(context)

    assert result.status == "success"
    assert result.output["mermaid_diagram"]


async def test_all_agents_have_unique_names(llm_service):
    agents = [
        APIAgent(llm_service),
        ArchitectureAgent(llm_service),
        DatabaseAgent(llm_service),
        DevOpsAgent(llm_service),
        RequirementsAgent(llm_service),
    ]
    names = {agent.name for agent in agents}
    assert len(names) == len(agents)


async def test_api_agent_revision_preserves_existing_and_issues(
    provider, llm_service, make_context
):
    """A targeted revision shows the existing artifact + reviewer issues and
    forbids a from-scratch regeneration."""
    agent = APIAgent(llm_service)
    context = make_context("Food delivery.")
    context.requirements = requirements_output()
    context.architecture = architecture_output()
    context.database = database_output()
    existing = api_output()
    revision = RevisionInstruction(
        artifact="api",
        existing=existing,
        issues=[
            {
                "artifact": "api",
                "severity": "blocking",
                "problem": "Endpoint X conflicts with database schema.",
                "expected": "users.id",
                "actual": "user_id",
                "fix": "Align the field name.",
            }
        ],
    )
    provider.set_responses([json.dumps(api_output())])

    await agent.run(context, revision=revision)

    user_prompt = provider.calls[0][1]
    assert "REVISION TASK — API" in user_prompt
    assert "preserve everything valid" in user_prompt
    assert "Endpoint X conflicts with database schema." in user_prompt
    assert "Do NOT regenerate the artifact from scratch" in user_prompt
    assert "Do NOT introduce new inconsistencies" in user_prompt
