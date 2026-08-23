from __future__ import annotations

from typing import Any

import httpx
import pytest

from agentic_core.config import Settings
from agentic_core.schemas import ProjectContext
from agentic_core.supabase_persistence import (
    SupabaseArtifactStore,
    SupabaseAuthenticationError,
    SupabaseConflictError,
    SupabaseGateway,
    SupabaseIdempotencyConflictError,
    SupabasePersistenceError,
    SupabaseProjectStore,
    SupabaseQuotaError,
)


class FakeGateway:
    def __init__(self):
        self.selected: list[dict[str, Any]] = []
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.inserts: list[tuple[str, Any]] = []

    def select(self, *_args, **_kwargs):
        return self.selected

    def rpc(self, name: str, payload: dict[str, Any]):
        self.calls.append((name, payload))
        if name == "claim_generation_stage_idempotent":
            return [
                {
                    "lease_token": payload["p_lease_token"],
                    "context_version": 5,
                }
            ]
        if name == "commit_generation_stage":
            return 6
        return 5

    def insert(self, table: str, rows: Any, **_kwargs):
        self.inserts.append((table, rows))
        return []


def test_gateway_does_not_corrupt_canonical_business_fields(monkeypatch):
    gateway = object.__new__(SupabaseGateway)
    captured: list[Any] = []

    def fake_request(_method, _path, **kwargs):
        captured.append(kwargs["payload"])
        return []

    monkeypatch.setattr(gateway, "_request", fake_request)
    canonical = {
        "authorization": {"type": "bearer"},
        "schema": {
            "properties": {
                "token": {"type": "string"},
                "password": {"type": "string"},
            }
        },
    }

    gateway.insert("projects", {"context": canonical})
    gateway.update("projects", {"context": canonical}, filters={"id": "eq.p1"})

    assert captured == [{"context": canonical}, {"context": canonical}]


def test_project_load_rejects_embedded_id_mismatch():
    gateway = FakeGateway()
    gateway.selected = [
        {
            "id": "proj_expected",
            "context_version": 0,
            "context": {
                "project_id": "proj_victim",
                "business_idea": "tampered",
            },
        }
    ]

    with pytest.raises(SupabasePersistenceError, match="identity"):
        SupabaseProjectStore(gateway).load("proj_expected", "user-1")


def test_project_save_uses_owner_scoped_version_rpc_without_upsert():
    gateway = FakeGateway()
    context = ProjectContext(
        project_id="proj_1",
        business_idea="API with password and token schema fields",
        api={"authorization": "Bearer", "token": {"type": "string"}},
    )
    context.persistence_version = 4

    SupabaseProjectStore(gateway).save(context, "user-1")

    name, payload = gateway.calls[-1]
    assert name == "save_project_context"
    assert payload["p_project_id"] == "proj_1"
    assert payload["p_user_id"] == "user-1"
    assert payload["p_expected_version"] == 4
    assert payload["p_context"]["api"]["authorization"] == "Bearer"
    assert payload["p_context"]["api"]["token"] == {"type": "string"}
    assert context.persistence_version == 5
    assert gateway.inserts == []


def test_stage_claim_is_versioned_and_returns_lease():
    gateway = FakeGateway()
    context = ProjectContext(project_id="proj_1", business_idea="idea")
    context.persistence_version = 4

    token = SupabaseProjectStore(gateway).claim_generation_stage(
        context, "user-1", "requirements"
    )

    assert token
    name, payload = gateway.calls[-1]
    assert name == "claim_generation_stage_idempotent"
    assert payload["p_expected_version"] == 4
    assert payload["p_expected_stage"] == "requirements"
    assert payload["p_lease_token"] == token
    assert context.persistence_version == 5


def test_claim_then_commit_uses_the_claimed_version():
    gateway = FakeGateway()
    context = ProjectContext(project_id="proj_1", business_idea="idea")
    context.persistence_version = 4
    store = SupabaseProjectStore(gateway)

    token = store.claim_generation_stage(context, "user-1", "requirements")
    store.commit_generation_stage(
        context,
        "user-1",
        "requirements",
        token,
        workflow_run_id="00000000-0000-0000-0000-000000000001",
        workflow_summary={"complete": False},
    )

    name, payload = gateway.calls[-1]
    assert name == "commit_generation_stage"
    assert payload["p_expected_version"] == 5
    assert payload["p_lease_token"] == token
    assert context.persistence_version == 6


def test_stage_claim_replays_the_same_token_after_a_lost_response():
    class FlakyGateway(FakeGateway):
        def __init__(self):
            super().__init__()
            self.attempts = 0

        def rpc(self, name: str, payload: dict[str, Any]):
            self.calls.append((name, payload.copy()))
            self.attempts += 1
            if self.attempts == 1:
                raise SupabasePersistenceError("response lost")
            return [
                {
                    "lease_token": payload["p_lease_token"],
                    "context_version": 5,
                }
            ]

    gateway = FlakyGateway()
    context = ProjectContext(project_id="proj_1", business_idea="idea")
    context.persistence_version = 4

    token = SupabaseProjectStore(gateway).claim_generation_stage(
        context, "user-1", "requirements"
    )

    assert gateway.attempts == 2
    assert gateway.calls[0][1]["p_lease_token"] == token
    assert gateway.calls[1][1]["p_lease_token"] == token
    assert context.persistence_version == 5


def test_artifact_structured_output_is_stored_verbatim():
    gateway = FakeGateway()
    structured = {
        "authorization": "OAuth2",
        "properties": {"password": {"type": "string"}},
    }

    SupabaseArtifactStore(gateway).write(
        "proj_1", "openapi.yaml", "openapi: 3.1.0", structured
    )

    _, row = gateway.inserts[-1]
    assert row["structured_data"] == structured


def test_production_fails_closed_without_supabase_or_with_anonymous_fallback():
    missing = Settings(_env_file=None, vercel=True, allow_anonymous_local=False)
    with pytest.raises(RuntimeError, match="Production requires SUPABASE"):
        missing.check_supabase_configuration()

    anonymous = Settings(
        _env_file=None,
        vercel=True,
        allow_anonymous_local=True,
        supabase_url="https://example.supabase.co",
        supabase_anon_key="anon",
        supabase_service_role_key="service",
    )
    with pytest.raises(RuntimeError, match="ALLOW_ANONYMOUS_LOCAL=false"):
        anonymous.check_supabase_configuration()


def test_request_deadline_is_clamped_below_host_limit():
    settings = Settings(_env_file=None, request_deadline_s=999)
    assert settings.effective_request_deadline_s == 210


def test_rpc_conflicts_have_a_distinct_exception(monkeypatch):
    gateway = object.__new__(SupabaseGateway)

    def fail(*_args, **_kwargs):
        raise SupabasePersistenceError(
            "Supabase request returned HTTP 400: generation_stage_conflict"
        )

    monkeypatch.setattr(gateway, "_request", fail)
    with pytest.raises(SupabaseConflictError):
        gateway.rpc("claim_generation_stage", {})


def test_rpc_daily_limit_has_a_distinct_exception(monkeypatch):
    gateway = object.__new__(SupabaseGateway)

    def fail(*_args, **_kwargs):
        raise SupabasePersistenceError(
            "Supabase request returned HTTP 400: daily_quota_exceeded"
        )

    monkeypatch.setattr(gateway, "_request", fail)
    with pytest.raises(SupabaseQuotaError):
        gateway.rpc("claim_user_quota", {})


def test_rpc_quota_fingerprint_conflict_has_a_distinct_exception(monkeypatch):
    gateway = object.__new__(SupabaseGateway)

    def fail(*_args, **_kwargs):
        raise SupabasePersistenceError(
            "Supabase request returned HTTP 400: quota_idempotency_conflict"
        )

    monkeypatch.setattr(gateway, "_request", fail)
    with pytest.raises(SupabaseIdempotencyConflictError):
        gateway.rpc("claim_user_quota", {})


def test_invalid_daily_limits_are_rejected():
    settings = Settings(_env_file=None, daily_project_limit=0)
    with pytest.raises(RuntimeError, match="DAILY_PROJECT_LIMIT"):
        settings.check_runtime_limits()


class _AuthClientStub:
    def __init__(self, result: httpx.Response | Exception):
        self._result = result

    def get(self, *_args, **_kwargs):
        if isinstance(self._result, Exception):
            raise self._result
        return self._result


def _auth_gateway(result: httpx.Response | Exception) -> SupabaseGateway:
    gateway = object.__new__(SupabaseGateway)
    gateway._anon_key = "public-anon-key"
    gateway._client = _AuthClientStub(result)
    return gateway


@pytest.mark.parametrize("status_code", [401, 403])
def test_verify_user_classifies_invalid_sessions_as_authentication_errors(status_code):
    gateway = _auth_gateway(httpx.Response(status_code))

    with pytest.raises(SupabaseAuthenticationError, match="invalid or expired"):
        gateway.verify_user("browser-access-token")


@pytest.mark.parametrize("status_code", [429, 500, 503])
def test_verify_user_classifies_auth_upstream_failures_as_service_errors(status_code):
    gateway = _auth_gateway(httpx.Response(status_code))

    with pytest.raises(SupabasePersistenceError, match="temporarily unavailable"):
        gateway.verify_user("browser-access-token")


def test_verify_user_classifies_auth_network_failures_as_service_errors():
    request = httpx.Request("GET", "https://example.supabase.co/auth/v1/user")
    gateway = _auth_gateway(httpx.ConnectError("connection failed", request=request))

    with pytest.raises(SupabasePersistenceError, match="temporarily unavailable"):
        gateway.verify_user("browser-access-token")


def test_verify_user_treats_success_without_identity_as_invalid_upstream_response():
    request = httpx.Request("GET", "https://example.supabase.co/auth/v1/user")
    gateway = _auth_gateway(httpx.Response(200, request=request, json={"email": "x"}))

    with pytest.raises(SupabasePersistenceError, match="invalid response"):
        gateway.verify_user("browser-access-token")
