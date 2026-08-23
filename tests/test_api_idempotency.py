from __future__ import annotations

import pytest

from agentic_core.api.idempotency import (
    IdempotencyConflictError,
    canonical_key,
    completed_operation,
    deterministic_project_id,
    operation_fingerprint,
    remember_operation,
    scoped_quota_key,
)
from agentic_core.schemas import ProjectContext


def test_idempotency_key_is_canonical_uuid():
    raw = "9B2E5D55-0DCD-4418-A191-EE1D495139A3"
    assert canonical_key(raw) == raw.lower()
    with pytest.raises(ValueError):
        canonical_key("not-a-uuid")


def test_project_id_is_stable_and_scoped_to_user():
    key = "9b2e5d55-0dcd-4418-a191-ee1d495139a3"
    assert deterministic_project_id("user-a", key) == deterministic_project_id(
        "user-a", key
    )
    assert deterministic_project_id("user-a", key) != deterministic_project_id(
        "user-b", key
    )
    assert scoped_quota_key("user-a", "generation_stage", "proj-a", key) != (
        scoped_quota_key("user-a", "generation_stage", "proj-b", key)
    )
    assert operation_fingerprint(
        "generation.next", "proj-a", "requirements"
    ) != operation_fingerprint(
        "generation.next", "proj-a", "architecture"
    )


def test_completed_receipt_prevents_duplicate_and_cross_request_reuse():
    context = ProjectContext(project_id="proj_test", business_idea="A library")
    key = "9b2e5d55-0dcd-4418-a191-ee1d495139a3"
    fingerprint = operation_fingerprint("discovery.message", "hello")

    assert completed_operation(
        context, key, "discovery.message", fingerprint
    ) is None
    remember_operation(
        context,
        key,
        "discovery.message",
        fingerprint,
        {"status": "ready"},
    )
    assert completed_operation(
        context, key, "discovery.message", fingerprint
    ) == {"status": "ready"}

    with pytest.raises(IdempotencyConflictError):
        completed_operation(
            context,
            key,
            "discovery.message",
            operation_fingerprint("discovery.message", "different"),
        )


def test_receipt_history_is_bounded():
    context = ProjectContext(project_id="proj_test", business_idea="A library")
    for index in range(80):
        key = f"00000000-0000-4000-8000-{index:012d}"
        remember_operation(
            context,
            key,
            "generation.next",
            operation_fingerprint("generation.next", context.project_id),
        )

    receipts = context.generation_state["completed_operations"]
    assert len(receipts) == 64
    assert receipts[0]["key"].endswith("000000000016")


def test_oversized_receipt_result_is_discarded():
    context = ProjectContext(project_id="proj_test", business_idea="A library")
    key = "9b2e5d55-0dcd-4418-a191-ee1d495139a3"
    fingerprint = operation_fingerprint("discovery.message", "hello")

    remember_operation(
        context,
        key,
        "discovery.message",
        fingerprint,
        {"provider_output": "x" * 5000},
    )

    assert completed_operation(
        context, key, "discovery.message", fingerprint
    ) == {}
