"""Structured output of the Review Agent.

The reviewer returns compact, actionable issues. Only ``blocking`` issues may
trigger regeneration; ``warning`` and ``suggestion`` are informational. Every
blocking issue must cite the source and conflicting decisions so the
orchestrator can run a targeted revision instead of regenerating from scratch.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ArtifactName = Literal["requirements", "architecture", "database", "api", "devops"]


class ReviewIssue(BaseModel):
    artifact: ArtifactName
    severity: Literal["blocking", "warning", "suggestion"]
    problem: str = ""
    expected: str = ""
    actual: str = ""
    fix: str = ""
    # Evidence: where the contradiction lives so the fix can be targeted.
    source_artifact: str = ""
    source_decision: str = ""
    conflicting_artifact: str = ""
    conflicting_decision: str = ""


class ReviewOutput(BaseModel):
    status: Literal["approved", "needs_revision"]
    score: float = Field(ge=0.0, le=1.0)
    issues: list[ReviewIssue] = Field(default_factory=list)
    # Optional hint; the orchestrator derives regeneration targets from the
    # blocking issues (artifact field), falling back to this list.
    artifacts_to_regenerate: list[str] = Field(default_factory=list)