"""Structured output of the Database Agent."""

from __future__ import annotations

from typing import ClassVar

from pydantic import BaseModel, Field


class DBField(BaseModel):
    name: str
    type: str
    primary_key: bool = False
    foreign_key: str | None = None
    nullable: bool = False
    unique: bool = False
    indexed: bool = False


class DBEntity(BaseModel):
    name: str
    description: str
    fields: list[DBField] = Field(default_factory=list)


class DatabaseOutput(BaseModel):
    database_technology: str = ""
    entities: list[DBEntity] = Field(default_factory=list)
    relationships: list[str] = Field(default_factory=list)
    indexes: list[str] = Field(default_factory=list)
    constraints: list[str] = Field(default_factory=list)
    sql_schema: str = ""
    erd_mermaid: str = ""

    # The SQL DDL and ERD diagram are derived locally from the entities/fields
    # (see artifacts/render.py), so the model must never spend output tokens on
    # them. Excluding them from the JSON schema shown to the LLM guarantees the
    # response stays small. Kept as fields for compatibility with saved data.
    llm_exclude_fields: ClassVar[frozenset[str]] = frozenset({"sql_schema", "erd_mermaid"})