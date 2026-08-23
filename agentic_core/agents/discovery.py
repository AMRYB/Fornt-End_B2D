"""Discovery Agent — the human-facing intelligence layer.

Understands a vague business idea through an adaptive conversation, updates the
project context and decides whether enough information exists to start
engineering.
"""

from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel

from ..llm import LLMService
from ..prompts import PROMPTS, build_user_prompt
from ..schemas import DiscoveryOutput, ProjectContext
from .base import BaseAgent, RevisionInstruction
from .digest import dumps

_LIST_FIELDS = {
    "target_users",
    "user_roles",
    "business_goals",
    "core_features",
    "constraints",
    "assumptions",
    "integrations",
    "security_requirements",
    "performance_requirements",
    "deployment_requirements",
    "technology_preferences",
}
_STR_FIELDS = {
    "problem",
    "scope",
    "auth_requirement",
    "authorization_requirement",
    "payment_requirement",
    "notification_requirement",
}


def known_info_snapshot(context: ProjectContext) -> dict[str, Any]:
    """Current understanding as the canonical discovery field snapshot."""
    return {
        "problem": context.problem,
        "target_users": context.target_users,
        "user_roles": context.user_roles,
        "business_goals": context.business_goals,
        "core_features": context.core_features,
        "scope": context.scope,
        "constraints": context.constraints,
        "assumptions": context.assumptions,
        "integrations": context.integrations,
        "security_requirements": context.security_requirements,
        "performance_requirements": context.performance_requirements,
        "deployment_requirements": context.deployment_requirements,
        "technology_preferences": context.technology_preferences,
        "auth_requirement": context.auth_requirement,
        "authorization_requirement": context.authorization_requirement,
        "payment_requirement": context.payment_requirement,
        "notification_requirement": context.notification_requirement,
    }


def apply_known_information(context: ProjectContext, known: dict[str, Any]) -> None:
    """Overwrite context fields with the discovery agent's understanding."""
    for key, value in known.items():
        if value is None:
            continue
        if key in _LIST_FIELDS:
            items = value if isinstance(value, list) else [value]
            setattr(context, key, [str(item) for item in items if item])
        elif key in _STR_FIELDS:
            setattr(context, key, str(value))
        elif hasattr(context, key):
            setattr(context, key, value)
    context.touch()


def discovery_agent_message(output: DiscoveryOutput) -> str:
    """Human-readable agent turn to append to the transcript."""
    if output.status == "ready":
        return output.summary
    lines = [output.summary]
    lines.extend(q.question for q in output.questions)
    return "\n".join(lines)


def format_transcript(context: ProjectContext) -> str:
    turns = context.transcript[-10:]
    if not turns:
        return "(no conversation yet)"
    lines = []
    for turn in turns:
        message = turn.message
        if len(message) > 1500:
            message = message[:1500] + f"... ({len(turn.message) - 1500} chars omitted ...)"
        lines.append(f"[{turn.role}] {message}")
    return "\n".join(lines)


class DiscoveryAgent(BaseAgent):
    name = "discovery"
    output_schema: type[BaseModel] = DiscoveryOutput
    system_prompt = PROMPTS["discovery"].system

    def __init__(self, llm_service: LLMService, tracker=None):
        super().__init__(llm_service, tracker)

    async def _execute(
        self, context: ProjectContext, revision: RevisionInstruction | None = None
    ) -> DiscoveryOutput:
        user_prompt = build_user_prompt(
            "discovery",
            idea=context.business_idea,
            known_info=dumps(known_info_snapshot(context)),
            transcript=format_transcript(context),
        )
        return await self._llm.generate(
            self.system_prompt, user_prompt, DiscoveryOutput, stats=self._stats
        )

    def apply(self, context: ProjectContext, output: DiscoveryOutput) -> None:
        apply_known_information(context, output.known_information)