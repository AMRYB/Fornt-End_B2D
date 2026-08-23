"""Structured output of the DevOps Agent."""

from __future__ import annotations

from pydantic import BaseModel, Field


class DevopsOutput(BaseModel):
    dockerfile: str = ""
    docker_compose: str = ""
    ci_cd_pipeline: str = ""
    github_actions: str = ""
    environment_variables: dict[str, str] = Field(default_factory=dict)
    deployment_strategy: str = ""
    health_checks: list[str] = Field(default_factory=list)
    logging: list[str] = Field(default_factory=list)
    monitoring: list[str] = Field(default_factory=list)
    secrets_management: str = ""