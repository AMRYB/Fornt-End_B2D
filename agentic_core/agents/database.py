"""Database Design Agent."""

from __future__ import annotations

from pydantic import BaseModel

from ..prompts import PROMPTS, build_user_prompt
from ..schemas import DatabaseOutput, ProjectContext
from .base import (
    BaseAgent,
    RevisionInstruction,
    project_context_payload,
    revision_instruction_text,
)
from .digest import condense_context, digest_architecture, digest_requirements, dumps


class DatabaseAgent(BaseAgent):
    name = "database"
    output_schema: type[BaseModel] = DatabaseOutput
    system_prompt = PROMPTS["database"].system

    async def _execute(
        self, context: ProjectContext, revision: RevisionInstruction | None = None
    ) -> DatabaseOutput:
        user_prompt = build_user_prompt(
            "database",
            project_context=dumps(condense_context(project_context_payload(context))),
            requirements=dumps(digest_requirements(context.requirements or {})),
            architecture=dumps(digest_architecture(context.architecture or {})),
        )
        if revision is not None:
            user_prompt += revision_instruction_text(revision)
        return await self._llm.generate(
            self.system_prompt, user_prompt, DatabaseOutput, stats=self._stats
        )