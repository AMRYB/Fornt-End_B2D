"""Structured output of the API Agent."""

from __future__ import annotations

from typing import Any, ClassVar, Literal

from pydantic import BaseModel, Field

HTTPMethod = Literal["GET", "POST", "PUT", "PATCH", "DELETE"]


class APIEndpoint(BaseModel):
    method: HTTPMethod
    path: str
    summary: str
    auth: str = "none"
    request_schema: dict[str, Any] | None = None
    response_schema: dict[str, Any] | None = None
    pagination: bool = False
    filters: list[str] = Field(default_factory=list)


class APIOutput(BaseModel):
    endpoints: list[APIEndpoint] = Field(default_factory=list)
    authentication: str = ""
    authorization: str = ""
    error_handling: list[str] = Field(default_factory=list)
    pagination: str = ""
    filtering: str = ""
    openapi_spec: dict[str, Any] = Field(default_factory=dict)

    # The OpenAPI document is derived locally from the endpoints (see
    # artifacts/render.py), so the model must never spend output tokens on it.
    # Excluding it from the JSON schema shown to the LLM guarantees a small
    # response. Kept as a field for compatibility with saved data.
    llm_exclude_fields: ClassVar[frozenset[str]] = frozenset({"openapi_spec"})