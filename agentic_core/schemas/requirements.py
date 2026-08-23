"""Structured output of the Requirements Agent."""

from __future__ import annotations

from pydantic import BaseModel, Field


class RequirementsOutput(BaseModel):
    functional_requirements: list[str] = Field(default_factory=list)
    non_functional_requirements: list[str] = Field(default_factory=list)
    user_stories: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)