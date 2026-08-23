"""Render structured agent outputs into human/ops-readable artifacts."""

from __future__ import annotations

import re
from typing import Any

import yaml

from ..schemas import (
    APIOutput,
    ArchitectureOutput,
    DatabaseOutput,
    DevopsOutput,
    ProjectContext,
    RequirementsOutput,
)


def _ul(items: list[str]) -> str:
    return "\n".join(f"- {item}" for item in items) if items else "- _none_"


def _section(title: str, body: str) -> str:
    return f"\n## {title}\n\n{body}\n"


def render_overview(context: ProjectContext) -> str:
    return (
        f"# Project Overview\n\n"
        f"- **Project ID:** `{context.project_id}`\n"
        f"- **Status:** `{context.status}`\n"
        f"\n"
        f"## Business Idea\n\n{context.business_idea}\n"
        + _section("Problem", context.problem or "_not specified_")
        + _section("Target Users", _ul(context.target_users))
        + _section("User Roles", _ul(context.user_roles))
        + _section("Business Goals", _ul(context.business_goals))
        + _section("Core Features", _ul(context.core_features))
        + _section("Scope", context.scope or "_not specified_")
        + _section("Constraints", _ul(context.constraints))
        + _section("Assumptions", _ul(context.assumptions))
        + _section("Integrations", _ul(context.integrations))
        + _section("Security Requirements", _ul(context.security_requirements))
        + _section("Performance Requirements", _ul(context.performance_requirements))
        + _section("Deployment Requirements", _ul(context.deployment_requirements))
        + _section("Technology Preferences", _ul(context.technology_preferences))
        + _section(
            "Auth & Payments",
            (
                f"- Authentication: {context.auth_requirement or '_none_'}\n"
                f"- Authorization: {context.authorization_requirement or '_none_'}\n"
                f"- Payments: {context.payment_requirement or '_none_'}\n"
                f"- Notifications: {context.notification_requirement or '_none_'}\n"
            ),
        )
    )


def render_requirements(output: RequirementsOutput) -> str:
    return (
        "# Requirements Specification\n\n"
        "## Functional Requirements\n\n"
        f"{_ul(output.functional_requirements)}\n"
        + _section("Non-Functional Requirements", _ul(output.non_functional_requirements))
        + _section("User Stories", _ul(output.user_stories))
        + _section("Acceptance Criteria", _ul(output.acceptance_criteria))
        + _section("Constraints", _ul(output.constraints))
        + _section("Assumptions", _ul(output.assumptions))
    )


def render_architecture(output: ArchitectureOutput) -> str:
    components = "\n".join(
        f"- **{c.name}** ({c.type}, {c.technology}) — {c.description}"
        for c in output.system_components
    )
    stack = "\n".join(f"- {k}: {v}" for k, v in output.technology_stack.items())
    diagram = output.mermaid_diagram or "_not provided_"
    return (
        "# System Architecture\n\n"
        f"## System Components\n\n{components}\n"
        + _section("Communication", _ul(output.communication))
        + _section("Authentication", output.authentication or "_not specified_")
        + _section("Security", _ul(output.security))
        + _section("Scalability", _ul(output.scalability))
        + _section("Technology Stack", stack)
        + _section("Deployment Architecture", output.deployment_architecture or "_not specified_")
        + _section("Architecture Diagram", f"```mermaid\n{diagram}\n```\n")
    )


def render_architecture_mmd(output: ArchitectureOutput) -> str:
    return output.mermaid_diagram


def render_database_markdown(output: DatabaseOutput) -> str:
    sections: list[str] = [
        "# Database Design\n\n",
        f"## Database Technology\n\n{output.database_technology}\n",
        "## Entities\n",
    ]
    for entity in output.entities:
        rows = "\n".join(
            f"| {f.name} | {f.type} | {'PK' if f.primary_key else ''} | "
            f"{f.foreign_key or ''} | {'NOT NULL' if not f.nullable else 'NULL'} | "
            f"{'UNIQUE' if f.unique else ''} | {'IDX' if f.indexed else ''} |"
            for f in entity.fields
        )
        header = "| Field | Type | PK | FK | Nullable | Unique | Indexed |\n|---|---|---|---|---|---|---|"
        sections.append(f"\n### {entity.name}\n\n{entity.description}\n\n{header}\n{rows}\n")
    sections.append(_section("Relationships", _ul(output.relationships)))
    sections.append(_section("Indexes", _ul(output.indexes)))
    sections.append(_section("Constraints", _ul(output.constraints)))
    sections.append(_section("ERD", f"```mermaid\n{render_erd(output)}\n```\n"))
    return "\n".join(sections)


def _table_order(entities: list) -> list[str]:
    """Order entities so referenced (parent) tables are created first."""
    names = {e.name for e in entities}
    by_name = {e.name: e for e in entities}

    def refs(e) -> list[str]:
        out = []
        for f in e.fields:
            if f.foreign_key and f.foreign_key.split(".")[0] in names:
                parent = f.foreign_key.split(".")[0]
                if parent not in out:
                    out.append(parent)
        return out

    ordered: list[str] = []
    visited: set[str] = set()

    def visit(name: str) -> None:
        if name in visited:
            return
        visited.add(name)
        for parent in refs(by_name[name]):
            visit(parent)
        ordered.append(name)

    for entity in entities:
        visit(entity.name)
    return ordered


def _sql_from_entities(entities: list) -> str:
    """Derive executable SQL DDL from the entity/field definitions."""
    by_name = {e.name: e for e in entities}
    statements: list[str] = []
    for name in _table_order(entities):
        entity = by_name[name]
        columns: list[str] = []
        for f in entity.fields:
            parts = [f.name, f.type or "TEXT"]
            if f.primary_key:
                parts.append("PRIMARY KEY")
            if f.foreign_key:
                parent, _, parent_field = f.foreign_key.partition(".")
                parts.append(f"REFERENCES {parent}({parent_field or 'id'})")
            if not f.nullable:
                parts.append("NOT NULL")
            if f.unique and not f.primary_key:
                parts.append("UNIQUE")
            columns.append(" ".join(parts))
        if not columns:
            continue
        statements.append(f"CREATE TABLE {name} (\n  " + ",\n  ".join(columns) + "\n);")
        for f in entity.fields:
            if f.indexed and not f.primary_key and not f.unique and f.foreign_key is None:
                statements.append(f"CREATE INDEX idx_{name}_{f.name} ON {name} ({f.name});")
    return "\n\n".join(statements)


def _erd_from_entities(entities: list) -> str:
    """Derive a Mermaid erDiagram from entity fields and foreign keys."""
    if not entities:
        return ""
    lines = ["erDiagram"]
    for e in entities:
        if e.fields:
            lines.append(f"  {e.name} {{")
            for f in e.fields:
                lines.append(f"    {f.type or 'TEXT'} {f.name}")
            lines.append("  }")
    for e in entities:
        for f in e.fields:
            if f.foreign_key and "." in f.foreign_key:
                parent, _ = f.foreign_key.split(".", 1)
                lines.append(f'  {parent} ||--o{{ {e.name} : ""')
    return "\n".join(lines)


def render_database_sql(output: DatabaseOutput) -> str:
    return output.sql_schema or _sql_from_entities(output.entities)


def render_erd(output: DatabaseOutput) -> str:
    return output.erd_mermaid or _erd_from_entities(output.entities)


def _operation_id(method: str, path: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9]+", "_", path).strip("_")
    return f"{method.lower()}_{safe or 'root'}"


def _openapi_from_endpoints(output: APIOutput) -> dict:
    """Derive a complete OpenAPI 3.0 document from the endpoint list."""
    paths: dict[str, Any] = {}
    requires_auth = False
    for ep in output.endpoints:
        path_item = paths.setdefault(ep.path, {})
        parameters: list[dict[str, Any]] = []
        if ep.pagination:
            parameters.append(
                {"name": "page", "in": "query", "schema": {"type": "integer"}}
            )
            parameters.append(
                {"name": "page_size", "in": "query", "schema": {"type": "integer"}}
            )
        for name in ep.filters:
            parameters.append({"name": name, "in": "query", "schema": {"type": "string"}})
        operation: dict[str, Any] = {
            "operationId": _operation_id(ep.method, ep.path),
            "summary": ep.summary,
            "parameters": parameters,
            "responses": {"200": {"description": "OK"}},
        }
        if ep.auth and ep.auth != "none":
            requires_auth = True
            operation["security"] = [{"bearerAuth": []}]
        if ep.request_schema:
            operation["requestBody"] = {
                "required": True,
                "content": {"application/json": {"schema": ep.request_schema}},
            }
        if ep.response_schema:
            operation["responses"]["200"]["content"] = {
                "application/json": {"schema": ep.response_schema}
            }
        path_item[ep.method.lower()] = operation

    spec: dict[str, Any] = {
        "openapi": "3.0.0",
        "info": {"title": "API", "version": "1.0.0"},
        "paths": paths,
    }
    if requires_auth:
        spec["components"] = {
            "securitySchemes": {"bearerAuth": {"type": "http", "scheme": "bearer"}}
        }
    return spec


def render_openapi(output: APIOutput) -> str:
    spec = output.openapi_spec or {}
    if not spec:
        spec = _openapi_from_endpoints(output)
    return yaml.safe_dump(spec, sort_keys=False, allow_unicode=True)


def render_api_markdown(output: APIOutput) -> str:
    endpoints = "\n".join(
        f"- **{e.method}** `{e.path}` — {e.summary} (auth: {e.auth})"
        + (f" [filters: {', '.join(e.filters)}]" if e.filters else "")
        + (" [paginated]" if e.pagination else "")
        for e in output.endpoints
    )
    return (
        "# API Design\n\n"
        f"## Endpoints\n\n{endpoints}\n"
        + _section("Authentication", output.authentication or "_not specified_")
        + _section("Authorization", output.authorization or "_not specified_")
        + _section("Error Handling", _ul(output.error_handling))
        + _section("Pagination", output.pagination or "_not specified_")
        + _section("Filtering", output.filtering or "_not specified_")
    )


def render_devops_markdown(output: DevopsOutput) -> str:
    env = "\n".join(f"- `{k}`: {v}" for k, v in output.environment_variables.items())
    return (
        "# DevOps Configuration\n\n"
        + _section("Deployment Strategy", output.deployment_strategy or "_not specified_")
        + _section("Health Checks", _ul(output.health_checks))
        + _section("Logging", _ul(output.logging))
        + _section("Monitoring", _ul(output.monitoring))
        + _section("Secrets Management", output.secrets_management or "_not specified_")
        + _section("CI/CD Pipeline", output.ci_cd_pipeline or "_not specified_")
        + _section("Environment Variables", env)
    )


def render_artifact_payload(artifact: str, output: Any) -> str:
    """Return the rendered text for a single artifact type."""
    if artifact == "requirements":
        return render_requirements(output)
    if artifact == "architecture":
        return render_architecture(output)
    if artifact == "database":
        return render_database_markdown(output)
    if artifact == "api":
        return render_api_markdown(output)
    if artifact == "devops":
        return render_devops_markdown(output)
    raise ValueError(f"Unknown artifact: {artifact}")


def render_all(context: ProjectContext) -> dict[str, str]:
    """Render the complete artifact set for a finished project."""
    files: dict[str, str] = {"overview.md": render_overview(context)}

    if context.requirements:
        output = RequirementsOutput.model_validate(context.requirements)
        files["requirements.md"] = render_requirements(output)

    if context.architecture:
        output = ArchitectureOutput.model_validate(context.architecture)
        files["architecture.md"] = render_architecture(output)
        files["architecture.mmd"] = render_architecture_mmd(output)

    if context.database:
        output = DatabaseOutput.model_validate(context.database)
        files["database.md"] = render_database_markdown(output)
        files["database.sql"] = render_database_sql(output)
        files["erd.mmd"] = render_erd(output)

    if context.api:
        output = APIOutput.model_validate(context.api)
        files["api.md"] = render_api_markdown(output)
        files["openapi.yaml"] = render_openapi(output)

    if context.devops:
        output = DevopsOutput.model_validate(context.devops)
        files["devops.md"] = render_devops_markdown(output)
        files["Dockerfile"] = output.dockerfile
        files["docker-compose.yml"] = output.docker_compose
        files["github-actions.yml"] = output.github_actions

    return files