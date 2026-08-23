"""Structured output of the Architecture Agent."""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

ComponentType = Literal[
    "frontend", "backend", "service", "database", "external", "infrastructure"
]


class SystemComponent(BaseModel):
    name: str
    type: ComponentType
    description: str
    technology: str


class ArchitectureOutput(BaseModel):
    system_components: list[SystemComponent] = Field(default_factory=list)
    communication: list[str] = Field(default_factory=list)
    authentication: str = ""
    security: list[str] = Field(default_factory=list)
    scalability: list[str] = Field(default_factory=list)
    technology_stack: dict[str, str] = Field(default_factory=dict)
    deployment_architecture: str = ""
    mermaid_diagram: str = ""