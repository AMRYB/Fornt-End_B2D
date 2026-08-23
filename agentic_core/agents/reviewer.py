"""Review Agent — cross-agent consistency validation.

The reviewer receives ONLY compact digests (requirements summary, architecture
decisions, database schema summary, API contract summary, DevOps summary) — one
copy of each artifact, never the discovery transcript, previous reviews, or
repeated artifacts. It returns a compact structured decision. Only ``blocking``
issues can trigger regeneration, and every blocking issue must cite the exact
source and conflicting decision so the orchestrator can run a targeted
revision instead of regenerating from scratch.
"""

from __future__ import annotations

from pydantic import BaseModel

from ..prompts import PROMPTS, build_user_prompt
from ..schemas import ProjectContext, ReviewOutput
from .base import BaseAgent, RevisionInstruction
from .digest import (
    digest_api,
    digest_architecture,
    digest_database,
    digest_devops,
    digest_requirements,
    dumps,
)


class ReviewAgent(BaseAgent):
    name = "reviewer"
    output_schema: type[BaseModel] = ReviewOutput
    system_prompt = PROMPTS["reviewer"].system

    async def _execute(
        self, context: ProjectContext, revision: RevisionInstruction | None = None
    ) -> ReviewOutput:
        user_prompt = build_user_prompt(
            "reviewer",
            requirements=dumps(digest_requirements(context.requirements or {})),
            architecture=dumps(digest_architecture(context.architecture or {})),
            database=dumps(digest_database(context.database or {})),
            api=dumps(digest_api(context.api or {})),
            devops=dumps(digest_devops(context.devops or {})),
        )
        return await self._llm.generate(
            self.system_prompt, user_prompt, ReviewOutput, stats=self._stats
        )