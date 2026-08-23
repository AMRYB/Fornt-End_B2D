"""Small, durable HTTP idempotency receipts stored with a project context.

The browser supplies a UUID for each paid mutation.  A bounded receipt list in
``generation_state`` lets a later serverless invocation recognize a request
whose HTTP response was lost, without persisting user text or credentials in
the receipt itself.
"""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import datetime, timezone
from typing import Any, Mapping

from ..schemas import ProjectContext


_PROJECT_NAMESPACE = uuid.UUID("c85da6b2-0cc8-4c45-9142-17e69233ce34")
_QUOTA_NAMESPACE = uuid.UUID("c0cf2db5-b1b1-4935-8e05-4c18d54367f1")
_MAX_COMPLETED_OPERATIONS = 64
_MAX_RECEIPT_RESULT_BYTES = 4096


class IdempotencyConflictError(ValueError):
    """The same client key was reused for a different logical request."""


def canonical_key(value: str | None) -> str:
    """Validate and canonicalize the browser's ``Idempotency-Key`` UUID."""

    if not value or not value.strip():
        raise ValueError("Idempotency-Key is required")
    try:
        parsed = uuid.UUID(value.strip())
    except (AttributeError, ValueError) as exc:
        raise ValueError("Idempotency-Key must be a UUID") from exc
    return str(parsed)


def deterministic_project_id(user_id: str, key: str) -> str:
    """Give one create request one stable project identifier per account."""

    digest = uuid.uuid5(_PROJECT_NAMESPACE, f"{user_id}:project.create:{key}")
    return f"proj_{digest.hex[:24]}"


def scoped_quota_key(
    user_id: str, kind: str, project_scope: str, key: str
) -> str:
    """Prevent one client UUID from deduplicating usage across projects."""

    value = f"{user_id}:{kind}:{project_scope}:{key}"
    return str(uuid.uuid5(_QUOTA_NAMESPACE, value))


def operation_fingerprint(operation: str, *values: object) -> str:
    """Hash request semantics without writing the user's message to metadata."""

    digest = hashlib.sha256()
    for value in (operation, *values):
        encoded = str(value).encode("utf-8")
        digest.update(len(encoded).to_bytes(8, "big"))
        digest.update(encoded)
    return digest.hexdigest()


def completed_operation(
    context: ProjectContext,
    key: str,
    operation: str,
    fingerprint: str,
) -> dict[str, Any] | None:
    """Return a stored result, or reject cross-request reuse of one key."""

    entry = operation_receipt(context, key)
    if entry is None:
        return None
    if (
        entry.get("operation") != operation
        or entry.get("fingerprint") != fingerprint
    ):
        raise IdempotencyConflictError(
            "Idempotency-Key was already used for a different request"
        )
    result = entry.get("result")
    return dict(result) if isinstance(result, Mapping) else {}


def operation_receipt(
    context: ProjectContext, key: str
) -> dict[str, Any] | None:
    """Return the newest durable receipt for a client key, if one exists."""

    entries = context.generation_state.get("completed_operations") or []
    if not isinstance(entries, list):
        return None
    for entry in reversed(entries):
        if isinstance(entry, Mapping) and entry.get("key") == key:
            return dict(entry)
    return None


def remember_operation(
    context: ProjectContext,
    key: str,
    operation: str,
    fingerprint: str,
    result: Mapping[str, Any] | None = None,
) -> None:
    """Persist a compact completion receipt and keep its growth bounded."""

    existing = context.generation_state.get("completed_operations") or []
    if not isinstance(existing, list):
        existing = []
    # Slice before copying so a malformed/legacy list cannot force an
    # unbounded allocation merely because a new receipt is appended.
    recent = existing[-(_MAX_COMPLETED_OPERATIONS - 1) :]
    entries = [
        dict(entry)
        for entry in recent
        if isinstance(entry, Mapping) and entry.get("key") != key
    ]
    compact_result = dict(result or {})
    try:
        encoded_result = json.dumps(
            compact_result,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError):
        compact_result = {}
    else:
        if len(encoded_result) > _MAX_RECEIPT_RESULT_BYTES:
            compact_result = {}
    entries.append(
        {
            "key": key,
            "operation": operation,
            "fingerprint": fingerprint,
            "completed_at": datetime.now(timezone.utc).isoformat(),
            "result": compact_result,
        }
    )
    context.generation_state["completed_operations"] = entries[
        -_MAX_COMPLETED_OPERATIONS:
    ]


def discovery_lease_expiry(context: ProjectContext) -> datetime | None:
    """Parse the current Discovery lease; malformed markers remain blocking."""

    marker = context.generation_state.get("discovery_lease")
    if not isinstance(marker, Mapping):
        return None
    raw = marker.get("expires_at")
    try:
        expiry = datetime.fromisoformat(str(raw))
    except (TypeError, ValueError):
        return datetime.max.replace(tzinfo=timezone.utc)
    if expiry.tzinfo is None:
        expiry = expiry.replace(tzinfo=timezone.utc)
    return expiry
