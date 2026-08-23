"""Compact digests of upstream artifacts.

Downstream agents only need the *signal* from an upstream artifact — entity
names and fields, component technologies, endpoint contracts — not the full
serialized output (which can be tens of KB for large projects). Passing compact
digests keeps every Cursor agent run smaller, which is the single biggest lever
on end-to-end wall-clock time: a smaller prompt means a shorter generation and
fewer validation failures (and therefore fewer expensive repair runs).

All digests are serialized compactly (no indentation) — indentation alone
inflated prompts by ~25-40%.
"""

from __future__ import annotations

import json
from typing import Any


def dumps(value: Any) -> str:
    """Compact JSON serialization for prompt embedding (no indentation)."""
    return json.dumps(value, separators=(",", ":"), ensure_ascii=False)


def _cap(items: list[Any], limit: int, kind: str = "items") -> list[Any]:
    if len(items) <= limit:
        return items
    return items[:limit] + [f"... ({len(items) - limit} more {kind} omitted ...)"]


def _cap_text(value: str, limit: int = 600) -> str:
    if len(value) <= limit:
        return value
    return value[:limit] + f"... ({len(value) - limit} chars omitted ...)"


def condense_context(payload: dict[str, Any]) -> dict[str, Any]:
    """Cap the discovery context snapshot fed into engineering prompts."""
    out: dict[str, Any] = {}
    for key, value in payload.items():
        if isinstance(value, list):
            out[key] = _cap(value, 12)
        elif isinstance(value, str):
            out[key] = _cap_text(value, 600)
        else:
            out[key] = value
    return out


def digest_requirements(req: dict[str, Any]) -> dict[str, Any]:
    """Keep FRs/NFRs/constraints; drop derived stories, ACs and assumptions."""
    if not req:
        return {}
    return {
        "functional_requirements": _cap(list(req.get("functional_requirements") or []), 12, "FRs"),
        "non_functional_requirements": _cap(
            list(req.get("non_functional_requirements") or []), 8, "NFRs"
        ),
        "constraints": _cap(list(req.get("constraints") or []), 6),
    }


def digest_architecture(arch: dict[str, Any]) -> dict[str, Any]:
    """Keep components (name/type/tech), comms, auth, stack; drop the diagram."""
    if not arch:
        return {}
    components: list[dict[str, Any]] = []
    for c in arch.get("system_components") or []:
        components.append(
            {
                "name": c.get("name"),
                "type": c.get("type"),
                "technology": c.get("technology"),
                "description": _cap_text(c.get("description") or "", 200),
            }
        )
    return {
        "system_components": _cap(components, 10, "components"),
        "communication": _cap(list(arch.get("communication") or []), 4),
        "authentication": arch.get("authentication"),
        "technology_stack": arch.get("technology_stack") or {},
    }


def digest_database(db: dict[str, Any]) -> dict[str, Any]:
    """Keep entities and the fields that matter for cross-artifact consistency
    (name/type/primary_key/foreign_key). Drop raw SQL, ERD and non-essential
    field flags so the prompt stays small."""
    if not db:
        return {}
    entities: list[dict[str, Any]] = []
    for e in db.get("entities") or []:
        fields = []
        for f in e.get("fields") or []:
            fields.append(
                {
                    "name": f.get("name"),
                    "type": f.get("type"),
                    "primary_key": f.get("primary_key"),
                    "foreign_key": f.get("foreign_key"),
                }
            )
        entities.append(
            {
                "name": e.get("name"),
                "description": _cap_text(e.get("description") or "", 200),
                "fields": _cap(fields, 16, "fields"),
            }
        )
    return {
        "database_technology": db.get("database_technology"),
        "entities": _cap(entities, 10, "entities"),
        "relationships": _cap(list(db.get("relationships") or []), 6),
        "constraints": _cap(list(db.get("constraints") or []), 6),
    }


def digest_api(api: dict[str, Any]) -> dict[str, Any]:
    """Keep endpoint contracts (method/path/summary/auth); drop the large
    OpenAPI document and non-essential endpoint metadata."""
    if not api:
        return {}
    endpoints: list[dict[str, Any]] = []
    for e in api.get("endpoints") or []:
        endpoints.append(
            {
                "method": e.get("method"),
                "path": e.get("path"),
                "summary": _cap_text(e.get("summary") or "", 100),
                "auth": e.get("auth"),
            }
        )
    return {
        "endpoints": _cap(endpoints, 20, "endpoints"),
        "authentication": api.get("authentication"),
        "authorization": _cap_text(api.get("authorization") or "", 300),
    }


def digest_devops(devops: dict[str, Any]) -> dict[str, Any]:
    """Keep the config files (capped) plus strategy; used by the reviewer.

    The reviewer cross-checks consistency (deployment strategy, health checks,
    tech alignment), not file correctness, so the heavy Dockerfile / compose /
    GitHub Actions contents are heavily truncated to keep the review prompt
    small.
    """
    if not devops:
        return {}
    return {
        "dockerfile": _cap_text(devops.get("dockerfile") or "", 300),
        "docker_compose": _cap_text(devops.get("docker_compose") or "", 300),
        "github_actions": _cap_text(devops.get("github_actions") or "", 300),
        "deployment_strategy": _cap_text(devops.get("deployment_strategy") or "", 300),
        "health_checks": _cap(list(devops.get("health_checks") or []), 4),
        "logging": _cap(list(devops.get("logging") or []), 3),
        "monitoring": _cap(list(devops.get("monitoring") or []), 3),
        "secrets_management": _cap_text(devops.get("secrets_management") or "", 200),
    }