"""Agents: base abstraction plus the discovery/engineering/review agents."""

from __future__ import annotations

from collections.abc import Mapping

from ..llm import LLMService
from .api import APIAgent
from .architecture import ArchitectureAgent
from .base import (
    AgentResult,
    BaseAgent,
    RevisionInstruction,
    project_context_payload,
    revision_instruction_text,
)
from .database import DatabaseAgent
from .devops import DevOpsAgent
from .discovery import (
    DiscoveryAgent,
    apply_known_information,
    discovery_agent_message,
    format_transcript,
    known_info_snapshot,
)
from .requirements import RequirementsAgent
from .reviewer import ReviewAgent

__all__ = [
    "APIAgent",
    "AgentResult",
    "ArchitectureAgent",
    "BaseAgent",
    "DatabaseAgent",
    "DevOpsAgent",
    "DiscoveryAgent",
    "RequirementsAgent",
    "ReviewAgent",
    "RevisionInstruction",
    "apply_known_information",
    "discovery_agent_message",
    "format_transcript",
    "known_info_snapshot",
    "project_context_payload",
    "revision_instruction_text",
]


def build_agents(
    llm_services: LLMService | Mapping[str, LLMService], tracker=None
) -> dict[str, BaseAgent]:
    """Instantiate the agent team with shared or per-agent LLM services."""
    def service_for(name: str) -> LLMService:
        if isinstance(llm_services, LLMService):
            return llm_services
        try:
            return llm_services[name]
        except KeyError as exc:
            raise ValueError(f"Missing LLM service for agent {name!r}") from exc

    return {
        "discovery": DiscoveryAgent(service_for("discovery"), tracker),
        "requirements": RequirementsAgent(service_for("requirements"), tracker),
        "architecture": ArchitectureAgent(service_for("architecture"), tracker),
        "database": DatabaseAgent(service_for("database"), tracker),
        "api": APIAgent(service_for("api"), tracker),
        "devops": DevOpsAgent(service_for("devops"), tracker),
        "reviewer": ReviewAgent(service_for("reviewer"), tracker),
    }