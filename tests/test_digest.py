"""Tests for compact artifact digests used to keep agent prompts small."""

from __future__ import annotations

from agentic_core.agents.digest import (
    digest_api,
    digest_architecture,
    digest_database,
    digest_requirements,
)
from tests.helpers import (
    api_output,
    architecture_output,
    database_output,
    requirements_output,
)


def test_digest_database_keeps_entities_drops_verbose_fields():
    d = digest_database(database_output())
    assert any(e["name"] == "orders" for e in d["entities"])
    assert d["database_technology"] == "PostgreSQL"
    assert "sql_schema" not in d
    assert "erd_mermaid" not in d
    assert "indexes" not in d


def test_digest_api_keeps_endpoints_drops_openapi():
    d = digest_api(api_output())
    assert any(e["path"] == "/api/orders" and e["method"] == "POST" for e in d["endpoints"])
    assert "openapi_spec" not in d


def test_digest_requirements_keeps_frs_drops_derived_lists():
    d = digest_requirements(requirements_output())
    assert d["functional_requirements"]
    assert "user_stories" not in d
    assert "acceptance_criteria" not in d
    assert "assumptions" not in d


def test_digest_architecture_keeps_components_drops_diagram():
    d = digest_architecture(architecture_output())
    assert any(c["name"] == "API Backend" for c in d["system_components"])
    assert "mermaid_diagram" not in d
    assert "deployment_architecture" not in d


def test_digests_cap_overlong_lists_and_strings():
    big = requirements_output()
    big["functional_requirements"] = [f"FR-{i}: something to build" for i in range(50)]
    d = digest_requirements(big)
    assert any("omitted" in item for item in d["functional_requirements"])
    assert len(d["functional_requirements"]) < 50