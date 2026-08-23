"""Structured output of the Discovery Agent."""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


class MissingInfo(BaseModel):
    field: str
    importance: Literal["critical", "optional", "not_applicable"]
    reason: str


class DiscoveryQuestion(BaseModel):
    id: str
    question: str
    reason: str
    options: list[str] = Field(default_factory=list)


class DiscoveryOutput(BaseModel):
    status: Literal["needs_clarification", "ready"]
    confidence: float = Field(ge=0.0, le=1.0)
    summary: str
    known_information: dict[str, Any] = Field(default_factory=dict)
    missing_information: list[MissingInfo] = Field(default_factory=list)
    questions: list[DiscoveryQuestion] = Field(default_factory=list)