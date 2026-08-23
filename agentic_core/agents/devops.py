"""DevOps Engineer Agent."""

from __future__ import annotations

from pydantic import BaseModel

from ..prompts import PROMPTS, build_user_prompt
from ..schemas import DevopsOutput, ProjectContext
from .base import (
    BaseAgent,
    RevisionInstruction,
    project_context_payload,
    revision_instruction_text,
)
from .digest import (
    condense_context,
    digest_api,
    digest_architecture,
    digest_database,
    digest_requirements,
    dumps,
)


class DevOpsAgent(BaseAgent):
    name = "devops"
    output_schema: type[BaseModel] = DevopsOutput
    system_prompt = PROMPTS["devops"].system

    async def _execute(
        self, context: ProjectContext, revision: RevisionInstruction | None = None
    ) -> DevopsOutput:
        user_prompt = build_user_prompt(
            "devops",
            project_context=dumps(condense_context(project_context_payload(context))),
            requirements=dumps(digest_requirements(context.requirements or {})),
            architecture=dumps(digest_architecture(context.architecture or {})),
            database=dumps(digest_database(context.database or {})),
            api=dumps(digest_api(context.api or {})),
        )
        if revision is not None:
            user_prompt += revision_instruction_text(revision)
        return await self._llm.generate(
            self.system_prompt, user_prompt, DevopsOutput, stats=self._stats
        )