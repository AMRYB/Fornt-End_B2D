from __future__ import annotations

from agentic_core.agents import build_agents
from agentic_core.config import GEMINI_AGENT_NAMES, Settings
from agentic_core.llm import aclose_llm_services, create_agent_llm_services


def gemini_settings(tmp_path) -> Settings:
    return Settings(
        llm_provider="gemini",
        gemini_discovery_api_key="key-discovery",
        gemini_requirements_api_key="key-requirements",
        gemini_architecture_api_key="key-architecture",
        gemini_database_api_key="key-database",
        gemini_api_api_key="key-api",
        gemini_devops_api_key="key-devops",
        gemini_reviewer_api_key="key-reviewer",
        data_dir=tmp_path,
    )


async def test_gemini_builds_one_isolated_service_per_agent(tmp_path):
    services = create_agent_llm_services(gemini_settings(tmp_path))
    try:
        assert set(services) == set(GEMINI_AGENT_NAMES)
        assert len({id(service) for service in services.values()}) == 7
        assert len({id(service.provider) for service in services.values()}) == 7
        assert {
            service.provider._api_key for service in services.values()
        } == {
            "key-discovery",
            "key-requirements",
            "key-architecture",
            "key-database",
            "key-api",
            "key-devops",
            "key-reviewer",
        }
    finally:
        await aclose_llm_services(services)


async def test_build_agents_uses_matching_named_service(tmp_path):
    services = create_agent_llm_services(gemini_settings(tmp_path))
    try:
        agents = build_agents(services)
        assert set(agents) == set(GEMINI_AGENT_NAMES)
        for name in GEMINI_AGENT_NAMES:
            assert agents[name]._llm is services[name]
    finally:
        await aclose_llm_services(services)
