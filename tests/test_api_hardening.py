from __future__ import annotations

import asyncio
import importlib
import json
import threading

import httpx
import pytest
from fastapi import HTTPException

from agentic_core.config import get_settings
from agentic_core.orchestrator import DiscoveryError
from agentic_core.schemas import DiscoveryOutput, ProjectContext
from agentic_core.supabase_persistence import (
    SupabaseConflictError,
    SupabasePersistenceError,
    SupabaseQuotaError,
)


@pytest.fixture
def api_module(monkeypatch, tmp_path):
    """Import the production adapter with hermetic local-only configuration."""

    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("VERCEL", "0")
    monkeypatch.setenv("ALLOW_ANONYMOUS_LOCAL", "true")
    monkeypatch.setenv("SUPABASE_URL", "")
    monkeypatch.setenv("SUPABASE_ANON_KEY", "")
    monkeypatch.setenv("SUPABASE_SERVICE_ROLE_KEY", "")
    monkeypatch.setenv("LLM_PROVIDER", "cursor")
    monkeypatch.setenv("CURSOR_API_KEY", "test-only-key")
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    get_settings.cache_clear()
    return importlib.import_module("agentic_core.api.app")


@pytest.mark.asyncio
async def test_global_persistence_error_response_hides_internal_details(api_module):
    response = await api_module.supabase_error_handler(
        None,
        SupabasePersistenceError(
            "Supabase request returned HTTP 500: secret_table_constraint"
        ),
    )
    payload = json.loads(response.body)

    assert response.status_code == 503
    assert payload == {
        "detail": "A required service is temporarily unavailable. Please retry."
    }
    assert "secret_table_constraint" not in response.body.decode("utf-8")


@pytest.mark.asyncio
async def test_auth_upstream_failure_reaches_api_as_generic_503(
    api_module, monkeypatch
):
    class AuthEnabledSettings:
        auth_enabled = True

    class UnavailableGateway:
        def verify_user(self, _access_token):
            raise SupabasePersistenceError("LEAK_SENTINEL auth upstream failure")

    monkeypatch.setattr(api_module.services, "settings", AuthEnabledSettings())
    monkeypatch.setattr(api_module.services, "gateway", UnavailableGateway())

    transport = httpx.ASGITransport(app=api_module.app, raise_app_exceptions=False)
    async with httpx.AsyncClient(
        transport=transport, base_url="https://app.example"
    ) as client:
        response = await client.get(
            "/api/projects",
            headers={"Authorization": "Bearer browser-access-token"},
        )

    assert response.status_code == 503
    assert response.json() == {
        "detail": "A required service is temporarily unavailable. Please retry."
    }
    assert "LEAK_SENTINEL" not in response.text


@pytest.mark.asyncio
async def test_discovery_error_response_hides_provider_details(api_module, monkeypatch):
    context = ProjectContext(project_id="proj_1", business_idea="idea")
    released: list[tuple[str, str, str]] = []

    async def claim_discovery(current, _user_id, _idempotency_key):
        return "discovery-token", current

    async def claim_quota(
        _user, _kind, _idempotency_key, _project_scope, _fingerprint
    ):
        return None

    async def release_discovery(current, user_id, token):
        released.append((current.project_id, user_id, token))

    class FailingOrchestrator:
        async def discovery_turn(self, _context, _message):
            raise DiscoveryError(
                "Gemini API error 500: provider-internal-sensitive-detail"
            )

    monkeypatch.setattr(api_module, "_claim_discovery_turn", claim_discovery)
    monkeypatch.setattr(api_module, "_claim_quota", claim_quota)
    monkeypatch.setattr(api_module, "_release_discovery_turn", release_discovery)
    monkeypatch.setattr(api_module.services, "orchestrator", FailingOrchestrator())

    user = api_module.CurrentUser(id="00000000-0000-0000-0000-000000000001")
    with pytest.raises(HTTPException) as caught:
        await api_module._discovery_turn(context, "hello", user)

    assert caught.value.status_code == 502
    assert caught.value.detail == (
        "The discovery agent could not complete this turn. Please retry."
    )
    assert "provider-internal-sensitive-detail" not in caught.value.detail
    assert released == [
        ("proj_1", "00000000-0000-0000-0000-000000000001", "discovery-token")
    ]


@pytest.mark.asyncio
async def test_generation_cancellation_shields_lease_release_and_reraises(
    api_module, monkeypatch
):
    context = ProjectContext(
        project_id="proj_1",
        business_idea="idea",
        status="generating",
        generation_state={
            "next_stage": "requirements",
            "workflow_run_id": "00000000-0000-0000-0000-000000000010",
        },
    )
    release_calls: list[tuple] = []
    release_tasks: list[asyncio.Task] = []
    generation_started = asyncio.Event()
    keep_running = asyncio.Event()

    class ProjectStoreStub:
        def claim_generation_stage(self, _context, _owner_id, _stage):
            return "stage-lease-token"

    class CancelledOrchestrator:
        async def generate_next(self, _context):
            generation_started.set()
            await keep_running.wait()
            raise AssertionError("generation should have been cancelled")

    async def load(_project_id, _user_id):
        return context

    async def claim_quota(
        _user, _kind, _idempotency_key, _project_scope, _fingerprint
    ):
        return None

    async def release_generation(*args):
        release_tasks.append(asyncio.current_task())
        await asyncio.sleep(0)
        release_calls.append(args)

    monkeypatch.setattr(api_module, "_load", load)
    monkeypatch.setattr(api_module, "_claim_quota", claim_quota)
    monkeypatch.setattr(api_module, "_release_generation_lease", release_generation)
    monkeypatch.setattr(api_module.services, "project_store", ProjectStoreStub())
    monkeypatch.setattr(api_module.services, "workflow_store", None)
    monkeypatch.setattr(api_module.services, "orchestrator", CancelledOrchestrator())

    user = api_module.CurrentUser(id="00000000-0000-0000-0000-000000000001")
    request_task = asyncio.create_task(api_module.generation_next("proj_1", user))
    await asyncio.wait_for(generation_started.wait(), timeout=1)
    request_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request_task

    assert request_task.cancelled()
    assert len(release_calls) == 1
    assert release_calls[0] == (
        "proj_1",
        "00000000-0000-0000-0000-000000000001",
        "stage-lease-token",
        "00000000-0000-0000-0000-000000000010",
        "requirements",
        "request_cancelled",
    )
    assert release_tasks[0] is not request_task


@pytest.mark.asyncio
async def test_generation_quota_cancellation_also_releases_owned_lease(
    api_module, monkeypatch
):
    context = ProjectContext(
        project_id="proj_1",
        business_idea="idea",
        status="generating",
        generation_state={"next_stage": "requirements"},
    )
    release_calls: list[tuple] = []
    quota_started = asyncio.Event()
    keep_running = asyncio.Event()

    class ProjectStoreStub:
        def claim_generation_stage(self, _context, _owner_id, _stage):
            return "stage-lease-token"

    async def load(_project_id, _user_id):
        return context

    async def cancelled_quota(
        _user, _kind, _idempotency_key, _project_scope, _fingerprint
    ):
        quota_started.set()
        await keep_running.wait()
        raise AssertionError("quota check should have been cancelled")

    async def release_generation(*args):
        release_calls.append(args)

    monkeypatch.setattr(api_module, "_load", load)
    monkeypatch.setattr(api_module, "_claim_quota", cancelled_quota)
    monkeypatch.setattr(api_module, "_release_generation_lease", release_generation)
    monkeypatch.setattr(api_module.services, "project_store", ProjectStoreStub())
    monkeypatch.setattr(api_module.services, "workflow_store", None)

    user = api_module.CurrentUser(id="00000000-0000-0000-0000-000000000001")
    request_task = asyncio.create_task(api_module.generation_next("proj_1", user))
    await asyncio.wait_for(quota_started.wait(), timeout=1)
    request_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await request_task

    assert request_task.cancelled()
    assert release_calls == [
        (
            "proj_1",
            "00000000-0000-0000-0000-000000000001",
            "stage-lease-token",
            "",
            "requirements",
            "request_cancelled",
        )
    ]


@pytest.mark.asyncio
async def test_generation_commit_cancellation_releases_only_project_lease(
    api_module, monkeypatch
):
    context = ProjectContext(
        project_id="proj_1",
        business_idea="idea",
        status="generating",
        generation_state={"next_stage": "requirements"},
    )
    releases: list[tuple] = []
    commit_started = threading.Event()
    allow_commit_thread_to_finish = threading.Event()

    class ProjectStoreStub:
        def claim_generation_stage(self, _context, _owner_id, _stage):
            return "stage-lease-token"

        def commit_generation_stage(self, *_args, **_kwargs):
            commit_started.set()
            allow_commit_thread_to_finish.wait(timeout=2)

        def release_generation_stage(self, *args):
            releases.append(args)

    class SuccessfulOrchestrator:
        async def generate_next(self, _context):
            return {
                "stage": "requirements",
                "completed_now": ["requirements"],
                "next_stage": "architecture",
                "complete": False,
            }

    async def load(_project_id, _user_id):
        return context

    async def claim_quota(
        _user, _kind, _idempotency_key, _project_scope, _fingerprint
    ):
        return None

    monkeypatch.setattr(api_module, "_load", load)
    monkeypatch.setattr(api_module, "_claim_quota", claim_quota)
    monkeypatch.setattr(api_module.services, "project_store", ProjectStoreStub())
    monkeypatch.setattr(api_module.services, "workflow_store", None)
    monkeypatch.setattr(api_module.services, "orchestrator", SuccessfulOrchestrator())

    user = api_module.CurrentUser(id="00000000-0000-0000-0000-000000000001")
    request_task = asyncio.create_task(api_module.generation_next("proj_1", user))
    started = await asyncio.wait_for(
        asyncio.to_thread(commit_started.wait, 1), timeout=2
    )
    assert started
    request_task.cancel()
    allow_commit_thread_to_finish.set()
    with pytest.raises(asyncio.CancelledError):
        await request_task

    assert request_task.cancelled()
    assert releases == [
        (
            "proj_1",
            "00000000-0000-0000-0000-000000000001",
            "stage-lease-token",
            {
                "reason": "checkpoint_commit_cancelled",
                "stage": "requirements",
            },
        )
    ]


@pytest.mark.asyncio
async def test_completed_discovery_request_replays_without_provider_or_quota(
    api_module, monkeypatch
):
    discovery_snapshot = {
        "status": "needs_clarification",
        "confidence": 0.6,
        "summary": "A saved summary",
        "known_information": {},
        "missing_information": [],
        "questions": [
            {
                "id": "audience",
                "question": "Who uses it?",
                "reason": "Defines the user roles.",
                "options": ["Teams", "Individuals"],
            }
        ],
    }
    context = ProjectContext(
        project_id="proj_1",
        business_idea="idea",
        generation_state={"discovery_snapshot": discovery_snapshot},
    )
    key = "9b2e5d55-0dcd-4418-a191-ee1d495139a3"
    operation = "discovery.message"
    fingerprint = api_module.operation_fingerprint(
        operation, context.project_id, "hello"
    )
    api_module.remember_operation(
        context,
        key,
        operation,
        fingerprint,
    )
    synced: list[str] = []

    async def sync_projection(current, _user_id):
        synced.append(current.project_id)

    async def should_not_claim(*_args, **_kwargs):
        raise AssertionError("completed discovery must not claim a lease")

    monkeypatch.setattr(api_module, "_sync_transcript_projection", sync_projection)
    monkeypatch.setattr(api_module, "_claim_discovery_turn", should_not_claim)

    user = api_module.CurrentUser(id="00000000-0000-0000-0000-000000000001")
    response = await api_module._discovery_turn(
        context,
        "hello",
        user,
        idempotency_key=key,
        operation=operation,
        fingerprint=fingerprint,
    )

    assert response["discovery"] == discovery_snapshot
    assert synced == ["proj_1"]


@pytest.mark.asyncio
async def test_successful_discovery_persists_validated_ui_snapshot(
    api_module, monkeypatch
):
    context = ProjectContext(project_id="proj_1", business_idea="idea")
    output = DiscoveryOutput(
        status="needs_clarification",
        confidence=0.7,
        summary="We need to identify the primary audience.",
        questions=[
            {
                "id": "audience",
                "question": "Who will use the product?",
                "reason": "User roles shape the requirements.",
                "options": ["Businesses", "Consumers"],
            }
        ],
    )

    async def claim(current, _user_id, _key):
        current.generation_state["discovery_lease"] = {"token": "lease-token"}
        return "lease-token", current

    async def no_quota(*_args, **_kwargs):
        return None

    async def save(current, _user_id):
        assert current.generation_state["discovery_snapshot"]["questions"]

    class DiscoveryStub:
        async def discovery_turn(self, _context, _message):
            return output

        def generation_snapshot(self, _context):
            return {}

    monkeypatch.setattr(api_module, "_claim_discovery_turn", claim)
    monkeypatch.setattr(api_module, "_claim_quota", no_quota)
    monkeypatch.setattr(api_module, "_save", save)
    monkeypatch.setattr(api_module.services, "orchestrator", DiscoveryStub())
    user = api_module.CurrentUser(id="00000000-0000-0000-0000-000000000001")

    response = await api_module._discovery_turn(
        context,
        "hello",
        user,
        idempotency_key="9b2e5d55-0dcd-4418-a191-ee1d495139a3",
    )

    assert response["discovery"] == output.model_dump(mode="json")
    assert "discovery_lease" not in context.generation_state


@pytest.mark.asyncio
async def test_completed_generation_request_cannot_advance_next_stage(
    api_module, monkeypatch
):
    context = ProjectContext(
        project_id="proj_1",
        business_idea="idea",
        status="generating",
        generation_state={"next_stage": "architecture"},
    )
    key = "9b2e5d55-0dcd-4418-a191-ee1d495139a3"
    operation = "generation.next"
    fingerprint = api_module.operation_fingerprint(
        operation, context.project_id, "requirements"
    )
    api_module.remember_operation(
        context,
        key,
        operation,
        fingerprint,
        {
            "stage": "requirements",
            "completed_now": ["requirements"],
            "next_stage": "architecture",
            "complete": False,
        },
    )

    async def load(_project_id, _user_id):
        return context

    async def persist(_context):
        return ["requirements.md"]

    class MustNotRun:
        async def generate_next(self, _context):
            raise AssertionError("idempotent replay advanced the next stage")

        def generation_snapshot(self, current):
            return {"next_stage": current.generation_state.get("next_stage")}

    monkeypatch.setattr(api_module, "_load", load)
    monkeypatch.setattr(api_module, "_persist_artifacts", persist)
    monkeypatch.setattr(api_module.services, "orchestrator", MustNotRun())

    user = api_module.CurrentUser(id="00000000-0000-0000-0000-000000000001")
    response = await api_module.generation_next(
        "proj_1", user, idempotency_key=key
    )

    assert response["stage"] == "requirements"
    assert response["next_stage"] == "architecture"
    assert response["artifacts"] == ["requirements.md"]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "status", ["confirmed", "generating", "approved", "revised", "needs_attention"]
)
async def test_discovery_confirmation_replay_returns_current_state_without_writes(
    status, api_module, monkeypatch
):
    context = ProjectContext(
        project_id="proj_1",
        business_idea="idea",
        status=status,
        generation_state={"discovery_snapshot": {"status": "ready"}},
    )

    async def load(_project_id, _user_id):
        return context

    def must_not_confirm(_context):
        raise AssertionError("confirmation replay invoked the state transition")

    async def must_not_save(_context, _user_id):
        raise AssertionError("confirmation replay wrote an unchanged project")

    monkeypatch.setattr(api_module, "_load", load)
    monkeypatch.setattr(api_module.services.orchestrator, "confirm", must_not_confirm)
    monkeypatch.setattr(api_module, "_save", must_not_save)

    user = api_module.CurrentUser(id="00000000-0000-0000-0000-000000000001")
    response = await api_module.discovery_confirm("proj_1", user)

    assert response["status"] == status
    assert response["discovery"]["status"] == "ready"


@pytest.mark.asyncio
async def test_discovery_confirmation_reconciles_competing_success(
    api_module, monkeypatch
):
    stale = ProjectContext(
        project_id="proj_1",
        business_idea="idea",
        status="ready_for_confirmation",
    )
    durable = ProjectContext(
        project_id="proj_1",
        business_idea="idea",
        status="confirmed",
    )
    load_count = 0

    async def load(_project_id, _user_id):
        nonlocal load_count
        load_count += 1
        return stale if load_count == 1 else durable

    async def conflicting_save(_context, _user_id):
        raise SupabaseConflictError("project_write_conflict")

    monkeypatch.setattr(api_module, "_load", load)
    monkeypatch.setattr(api_module, "_save", conflicting_save)

    user = api_module.CurrentUser(id="00000000-0000-0000-0000-000000000001")
    response = await api_module.discovery_confirm("proj_1", user)

    assert response["status"] == "confirmed"
    assert load_count == 2


@pytest.mark.asyncio
async def test_discovery_confirmation_still_rejects_unready_project(
    api_module, monkeypatch
):
    context = ProjectContext(
        project_id="proj_1",
        business_idea="idea",
        status="discovery",
    )

    async def load(_project_id, _user_id):
        return context

    monkeypatch.setattr(api_module, "_load", load)
    user = api_module.CurrentUser(id="00000000-0000-0000-0000-000000000001")

    with pytest.raises(HTTPException) as caught:
        await api_module.discovery_confirm("proj_1", user)

    assert caught.value.status_code == 409
    assert context.status == "discovery"


@pytest.mark.asyncio
async def test_discovery_confirmation_rejects_live_turn(api_module, monkeypatch):
    context = ProjectContext(
        project_id="proj_1",
        business_idea="idea",
        status="ready_for_confirmation",
        generation_state={
            "discovery_lease": {
                "token": "lease-token",
                "expires_at": "2999-01-01T00:00:00+00:00",
            }
        },
    )

    async def load(_project_id, _user_id):
        return context

    class MustNotConfirm:
        def confirm(self, _context):
            raise AssertionError("confirmation overtook live discovery")

    monkeypatch.setattr(api_module, "_load", load)
    monkeypatch.setattr(api_module.services, "orchestrator", MustNotConfirm())
    user = api_module.CurrentUser(id="00000000-0000-0000-0000-000000000001")

    with pytest.raises(HTTPException) as caught:
        await api_module.discovery_confirm("proj_1", user)

    assert caught.value.status_code == 409
    assert "still running" in caught.value.detail


@pytest.mark.asyncio
async def test_same_discovery_operation_gets_retryable_pending_response(api_module):
    key = "9b2e5d55-0dcd-4418-a191-ee1d495139a3"
    context = ProjectContext(
        project_id="proj_1",
        business_idea="idea",
        generation_state={
            "discovery_lease": {
                "token": "lease-token",
                "idempotency_key": key,
                "expires_at": "2999-01-01T00:00:00+00:00",
            }
        },
    )

    with pytest.raises(HTTPException) as caught:
        await api_module._claim_discovery_turn(context, "user-1", key)

    assert caught.value.status_code == 425
    assert caught.value.headers == {"Retry-After": "2"}


@pytest.mark.asyncio
async def test_create_response_replay_does_not_consume_quota_again(
    api_module, monkeypatch
):
    user = api_module.CurrentUser(id="00000000-0000-0000-0000-000000000001")
    key = "9b2e5d55-0dcd-4418-a191-ee1d495139a3"
    idea = "A library"
    project_id = api_module.deterministic_project_id(user.id, key)
    context = ProjectContext(project_id=project_id, business_idea=idea)
    operation = "project.create"
    fingerprint = api_module.operation_fingerprint(operation, idea)
    api_module.remember_operation(
        context,
        key,
        operation,
        fingerprint,
    )

    class ExistingProjectStore:
        def load(self, requested_id, owner_id):
            assert requested_id == project_id
            assert owner_id == user.id
            return context

    async def no_quota(*_args, **_kwargs):
        raise AssertionError("completed create consumed project quota twice")

    async def sync_projection(_context, _user_id):
        return None

    monkeypatch.setattr(api_module.services, "project_store", ExistingProjectStore())
    monkeypatch.setattr(api_module, "_claim_quota", no_quota)
    monkeypatch.setattr(api_module, "_sync_transcript_projection", sync_projection)

    response = await api_module.create_project(
        api_module.CreateProjectRequest(business_idea=idea),
        user,
        idempotency_key=key,
    )

    assert response["project_id"] == project_id
    assert response["discovery"] is None


@pytest.mark.asyncio
async def test_quota_claim_is_bound_to_operation_key(api_module, monkeypatch):
    calls: list[tuple[str, dict]] = []

    class GatewayStub:
        def rpc(self, name, payload):
            calls.append((name, payload))
            return 1

    class SettingsStub:
        daily_project_limit = 5
        daily_discovery_limit = 30
        daily_generation_stage_limit = 40

    monkeypatch.setattr(api_module.services, "gateway", GatewayStub())
    monkeypatch.setattr(api_module.services, "settings", SettingsStub())
    user = api_module.CurrentUser(id="00000000-0000-0000-0000-000000000001")
    key = "9b2e5d55-0dcd-4418-a191-ee1d495139a3"
    fingerprint = api_module.operation_fingerprint(
        "discovery.message", "proj-1", "hello"
    )

    await api_module._claim_quota(
        user, "discovery", key, "proj-1", fingerprint
    )

    assert calls == [
        (
            "claim_user_quota",
            {
                "p_user_id": user.id,
                "p_kind": "discovery",
                "p_limit": 30,
                "p_idempotency_key": api_module.scoped_quota_key(
                    user.id, "discovery", "proj-1", key
                ),
                "p_fingerprint": fingerprint,
            },
        )
    ]


@pytest.mark.asyncio
async def test_generation_quota_fingerprint_includes_claimed_stage(
    api_module, monkeypatch
):
    context = ProjectContext(
        project_id="proj_1",
        business_idea="idea",
        status="generating",
        generation_state={"next_stage": "requirements"},
    )
    claims: list[tuple[str, str, str]] = []

    class ProjectStoreStub:
        def claim_generation_stage(self, _context, _owner_id, stage):
            assert stage == "requirements"
            return "stage-lease-token"

    async def load(_project_id, _user_id):
        return context

    async def reject_after_capture(
        _user, kind, _key, project_scope, fingerprint
    ):
        claims.append((kind, project_scope, fingerprint))
        raise SupabaseQuotaError("daily_quota_exceeded")

    async def release_generation(*_args):
        return None

    monkeypatch.setattr(api_module, "_load", load)
    monkeypatch.setattr(api_module, "_claim_quota", reject_after_capture)
    monkeypatch.setattr(
        api_module, "_release_generation_lease", release_generation
    )
    monkeypatch.setattr(
        api_module.services, "project_store", ProjectStoreStub()
    )
    monkeypatch.setattr(api_module.services, "workflow_store", None)

    user = api_module.CurrentUser(id="00000000-0000-0000-0000-000000000001")
    with pytest.raises(SupabaseQuotaError):
        await api_module.generation_next(
            "proj_1",
            user,
            idempotency_key="9b2e5d55-0dcd-4418-a191-ee1d495139a3",
        )

    assert claims == [
        (
            "generation_stage",
            "proj_1",
            api_module.operation_fingerprint(
                "generation.next", "proj_1", "requirements"
            ),
        )
    ]
