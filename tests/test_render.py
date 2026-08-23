"""Tests for artifact rendering, including derived OpenAPI/SQL/ERD fallbacks."""

from __future__ import annotations

from agentic_core.artifacts.render import (
    render_database_sql,
    render_erd,
    render_openapi,
)
from agentic_core.schemas import APIOutput, DatabaseOutput
from tests.helpers import api_output, database_output


def test_openapi_falls_back_to_endpoints_when_spec_omitted():
    payload = api_output() | {"openapi_spec": {}}
    out = APIOutput.model_validate(payload)
    text = render_openapi(out)
    assert "openapi: 3.0.0" in text
    assert "/api/restaurants" in text
    assert "/api/orders/{id}" in text
    assert "bearerAuth" in text  # secured endpoints add a bearer scheme


def test_openapi_uses_model_spec_when_provided():
    out = APIOutput.model_validate(api_output())  # helper ships an openapi_spec
    text = render_openapi(out)
    assert "Food Delivery API" in text
    assert "openapi: 3.0.0" in text


def test_sql_derived_from_entities_when_schema_omitted():
    payload = database_output() | {"sql_schema": "", "erd_mermaid": ""}
    out = DatabaseOutput.model_validate(payload)
    text = render_database_sql(out)
    assert "CREATE TABLE users" in text
    assert "CREATE TABLE orders" in text
    assert "REFERENCES users(id)" in text  # FK derived from field metadata
    assert "PRIMARY KEY" in text


def test_erd_derived_from_entities_when_diagram_omitted():
    payload = database_output() | {"sql_schema": "", "erd_mermaid": ""}
    out = DatabaseOutput.model_validate(payload)
    text = render_erd(out)
    assert text.startswith("erDiagram")
    assert "users ||--o{" in text
    assert "orders" in text


def test_sql_uses_model_schema_when_provided():
    out = DatabaseOutput.model_validate(database_output())  # helper ships sql
    assert "CREATE TABLE orders" in render_database_sql(out)