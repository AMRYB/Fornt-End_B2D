"""Requirements Agent."""

from __future__ import annotations

from pydantic import BaseModel

from ..prompts import PROMPTS, build_user_prompt
from ..schemas import ProjectContext, RequirementsOutput
from .base import (
    BaseAgent,
    RevisionInstruction,
    project_context_payload,
    revision_instruction_text,
)
from .digest import dumps


class RequirementsAgent(BaseAgent):
    name = "requirements"
    output_schema: type[BaseModel] = RequirementsOutput
    system_prompt = PROMPTS["requirements"].system

    async def _execute(
        self, context: ProjectContext, revision: RevisionInstruction | None = None
    ) -> RequirementsOutput:
        user_prompt = build_user_prompt(
            "requirements",
            project_context=dumps(project_context_payload(context)),
        )
        if revision is not None:
            user_prompt += revision_instruction_text(revision)
        return await self._llm.generate(
            self.system_prompt, user_prompt, RequirementsOutput, stats=self._stats
        )