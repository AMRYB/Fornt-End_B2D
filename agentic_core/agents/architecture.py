"""Architecture Agent."""

from __future__ import annotations

from pydantic import BaseModel

from ..prompts import PROMPTS, build_user_prompt
from ..schemas import ArchitectureOutput, ProjectContext
from .base import (
    BaseAgent,
    RevisionInstruction,
    project_context_payload,
    revision_instruction_text,
)
from .digest import condense_context, digest_requirements, dumps


class ArchitectureAgent(BaseAgent):
    name = "architecture"
    output_schema: type[BaseModel] = ArchitectureOutput
    system_prompt = PROMPTS["architecture"].system

    async def _execute(
        self, context: ProjectContext, revision: RevisionInstruction | None = None
    ) -> ArchitectureOutput:
        user_prompt = build_user_prompt(
            "architecture",
            project_context=dumps(condense_context(project_context_payload(context))),
            requirements=dumps(digest_requirements(context.requirements or {})),
        )
        if revision is not None:
            user_prompt += revision_instruction_text(revision)
        return await self._llm.generate(
            self.system_prompt, user_prompt, ArchitectureOutput, stats=self._stats
        )