"""Central project state shared across all agents."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

ProjectStatus = Literal[
    "discovery",
    "ready_for_confirmation",
    "confirmed",
    "generating",
    "approved",
    "revised",
    "needs_attention",
]


class DiscoveryTurn(BaseModel):
    role: Literal["user", "agent"]
    message: str
    timestamp: datetime = Field(default_factory=datetime.now)


class ProjectContext(BaseModel):
    project_id: str

    # Optimistic-lock version supplied by the durable store.  It is deliberately
    # excluded from serialized project/LLM payloads; Supabase keeps the trusted
    # version in its own relational column.
    persistence_version: int = Field(default=0, exclude=True, repr=False)

    business_idea: str

    problem: str | None = None
    target_users: list[str] = Field(default_factory=list)
    user_roles: list[str] = Field(default_factory=list)
    business_goals: list[str] = Field(default_factory=list)
    core_features: list[str] = Field(default_factory=list)
    scope: str | None = None

    constraints: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    integrations: list[str] = Field(default_factory=list)

    security_requirements: list[str] = Field(default_factory=list)
    performance_requirements: list[str] = Field(default_factory=list)
    deployment_requirements: list[str] = Field(default_factory=list)

    technology_preferences: list[str] = Field(default_factory=list)

    auth_requirement: str | None = None
    authorization_requirement: str | None = None
    payment_requirement: str | None = None
    notification_requirement: str | None = None

    business_analysis: dict[str, Any] | None = None
    requirements: dict[str, Any] | None = None
    architecture: dict[str, Any] | None = None
    database: dict[str, Any] | None = None
    api: dict[str, Any] | None = None
    devops: dict[str, Any] | None = None
    review: dict[str, Any] | None = None

    # Durable checkpoint for serverless generation.  The regular ``generate``
    # method still supports the original single-process workflow, while the API
    # advances this state one dependency-safe stage per request on Vercel.
    generation_state: dict[str, Any] = Field(default_factory=dict)

    status: ProjectStatus = "discovery"
    transcript: list[DiscoveryTurn] = Field(default_factory=list)
    updated_at: datetime = Field(default_factory=datetime.now)

    def add_turn(self, role: Literal["user", "agent"], message: str) -> None:
        self.transcript.append(DiscoveryTurn(role=role, message=message))
        self.updated_at = datetime.now()

    def touch(self) -> None:
        self.updated_at = datetime.now()
