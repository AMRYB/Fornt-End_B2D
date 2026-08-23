"""Authenticated FastAPI adapter between the browser and the seven-agent core.

Production requests use Supabase Auth plus durable Supabase stores. Local
development can still use the original SQLite/filesystem adapters with an
implicit local user, preserving the CLI and unit-test workflow.
"""

from __future__ import annotations

import asyncio
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse
from pydantic import BaseModel, Field

from ..agents import known_info_snapshot
from ..artifacts import render_all
from ..orchestrator import DiscoveryError, OrchestrationError
from ..schemas import ProjectContext
from ..supabase_persistence import (
    SupabaseAuthenticationError,
    SupabaseConflictError,
    SupabaseIdempotencyConflictError,
    SupabasePersistenceError,
    SupabaseQuotaError,
)
from .deps import services
from .idempotency import (
    IdempotencyConflictError,
    canonical_key,
    completed_operation,
    deterministic_project_id,
    discovery_lease_expiry,
    operation_receipt,
    operation_fingerprint,
    remember_operation,
    scoped_quota_key,
)


@dataclass(frozen=True)
class CurrentUser:
    id: str
    email: str = ""
    metadata: dict[str, Any] | None = None


@asynccontextmanager
async def lifespan(_app: FastAPI):
    yield
    await services.aclose()


app = FastAPI(title="Business to Development API", version="1.0.0", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=services.settings.cors_origins,
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "DELETE", "OPTIONS"],
    allow_headers=[
        "Authorization",
        "Content-Type",
        "Idempotency-Key",
        "X-Request-ID",
    ],
)


class CreateProjectRequest(BaseModel):
    business_idea: str = Field(min_length=1, max_length=20_000)


class MessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=20_000)


def _request_idempotency_key(raw: str | None) -> str:
    # Keep direct/local clients backward-compatible while production requires
    # an explicit browser-stable key for every paid mutation.
    if not isinstance(raw, str) or not raw.strip():
        if not services.settings.is_production:
            return str(uuid.uuid4())
        raise HTTPException(status_code=400, detail="Idempotency-Key is required")
    try:
        return canonical_key(raw)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


def _completed_request(
    context: ProjectContext,
    key: str,
    operation: str,
    fingerprint: str,
) -> dict[str, Any] | None:
    try:
        return completed_operation(context, key, operation, fingerprint)
    except IdempotencyConflictError as exc:
        raise HTTPException(
            status_code=409,
            detail="That request identifier was already used for another operation.",
        ) from exc


@app.exception_handler(SupabasePersistenceError)
async def supabase_error_handler(_request, _exc: SupabasePersistenceError):
    # PostgREST may include schema, constraint, or provider details in its
    # response body. Keep those details out of the public API response.
    return JSONResponse(
        status_code=503,
        content={
            "detail": "A required service is temporarily unavailable. Please retry."
        },
    )


@app.exception_handler(SupabaseConflictError)
async def supabase_conflict_handler(_request, _exc: SupabaseConflictError):
    return JSONResponse(
        status_code=409,
        content={
            "detail": (
                "This project changed in another request. Reload it and try again."
            )
        },
    )


@app.exception_handler(SupabaseIdempotencyConflictError)
async def supabase_idempotency_conflict_handler(
    _request, _exc: SupabaseIdempotencyConflictError
):
    return JSONResponse(
        status_code=409,
        content={
            "detail": (
                "That request identifier was already used for another operation."
            )
        },
    )


@app.exception_handler(SupabaseQuotaError)
async def supabase_quota_handler(_request, _exc: SupabaseQuotaError):
    return JSONResponse(
        status_code=429,
        content={
            "detail": (
                "Your daily AI usage allowance has been reached. "
                "Please continue tomorrow."
            )
        },
        headers={"Retry-After": "3600"},
    )


async def current_user(
    authorization: str | None = Header(default=None),
) -> CurrentUser:
    """Verify Supabase's bearer token or provide the local development user."""

    if not services.settings.auth_enabled:
        if not services.settings.allow_anonymous_local:
            raise HTTPException(status_code=503, detail="Authentication is not configured")
        return CurrentUser(
            id="00000000-0000-0000-0000-000000000000",
            email="local@b2d.invalid",
            metadata={"display_name": "Local user"},
        )

    scheme, _, token = (authorization or "").partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(status_code=401, detail="A valid sign-in session is required")
    try:
        payload = await asyncio.to_thread(services.gateway.verify_user, token.strip())
    except SupabaseAuthenticationError as exc:
        raise HTTPException(status_code=401, detail=str(exc)) from exc
    return CurrentUser(
        id=str(payload["id"]),
        email=str(payload.get("email") or ""),
        metadata=payload.get("user_metadata") or {},
    )


async def _claim_quota(
    user: CurrentUser,
    kind: str,
    idempotency_key: str,
    project_scope: str,
    fingerprint: str,
) -> None:
    """Atomically reserve one fingerprint-bound daily usage unit."""

    if services.gateway is None:
        return
    limits = {
        "project_create": services.settings.daily_project_limit,
        "discovery": services.settings.daily_discovery_limit,
        "generation_stage": services.settings.daily_generation_stage_limit,
    }
    try:
        limit = limits[kind]
    except KeyError as exc:  # pragma: no cover - developer contract
        raise RuntimeError(f"Unknown quota kind: {kind}") from exc
    await asyncio.to_thread(
        services.gateway.rpc,
        "claim_user_quota",
        {
            "p_user_id": user.id,
            "p_kind": kind,
            "p_limit": limit,
            "p_idempotency_key": scoped_quota_key(
                user.id, kind, project_scope, idempotency_key
            ),
            "p_fingerprint": fingerprint,
        },
    )


async def _load(project_id: str, user_id: str) -> ProjectContext:
    context = await asyncio.to_thread(services.project_store.load, project_id, user_id)
    if context is None:
        # A single 404 response avoids revealing whether a project exists for a
        # different account.
        raise HTTPException(status_code=404, detail="Project not found")
    return context


async def _sync_transcript_projection(
    context: ProjectContext, user_id: str
) -> None:
    # The canonical ProjectContext commit already succeeded.  A temporary
    # transcript projection failure must not make the browser repeat a paid LLM
    # call; the next successful save will idempotently resync every turn.
    try:
        await asyncio.to_thread(
            services.project_store.sync_transcript, context, user_id
        )
    except SupabasePersistenceError:
        pass


async def _save(context: ProjectContext, user_id: str) -> None:
    await asyncio.to_thread(services.project_store.save, context, user_id)
    await _sync_transcript_projection(context, user_id)


_DISCOVERY_STATUSES = {"discovery", "ready_for_confirmation"}
_CONFIRMATION_COMPLETE_STATUSES = {
    "confirmed",
    "generating",
    "approved",
    "revised",
    "needs_attention",
}


async def _claim_discovery_turn(
    context: ProjectContext, user_id: str, idempotency_key: str
) -> tuple[str, ProjectContext]:
    """Optimistically reserve one discovery turn before spending Gemini tokens."""

    if context.status not in _DISCOVERY_STATUSES:
        raise HTTPException(
            status_code=409,
            detail=f"Discovery is closed for a project in {context.status!r} status.",
        )

    now = datetime.now(timezone.utc)
    existing = context.generation_state.get("discovery_lease") or {}
    expires_text = existing.get("expires_at") if isinstance(existing, dict) else None
    try:
        expires_at = datetime.fromisoformat(str(expires_text))
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=timezone.utc)
    except (TypeError, ValueError):
        expires_at = now - timedelta(seconds=1)
    if expires_at > now:
        if (
            isinstance(existing, dict)
            and existing.get("idempotency_key") == idempotency_key
        ):
            raise HTTPException(
                status_code=425,
                detail="This discovery request is still running. Please retry shortly.",
                headers={"Retry-After": "2"},
            )
        raise SupabaseConflictError("A discovery turn is already running")

    token = str(uuid.uuid4())
    context.generation_state["discovery_lease"] = {
        "token": token,
        "idempotency_key": idempotency_key,
        "expires_at": (
            now
            + timedelta(
                seconds=services.settings.effective_request_deadline_s + 40
            )
        ).isoformat(),
    }
    # This CAS write is the claim. Two requests loaded at the same version may
    # both reach here, but only one can increment the durable version; the loser
    # receives 409 before invoking Gemini.
    try:
        await asyncio.to_thread(services.project_store.save, context, user_id)
    except SupabasePersistenceError:
        # Reconcile the serverless uncertainty case where the CAS committed but
        # the HTTP response was lost. The caller-generated token proves that
        # this exact request owns the durable claim.
        try:
            latest = await asyncio.to_thread(
                services.project_store.load, context.project_id, user_id
            )
        except SupabasePersistenceError:
            latest = None
        durable_lease = latest.generation_state.get("discovery_lease") if latest else None
        if isinstance(durable_lease, dict) and durable_lease.get("token") == token:
            return token, latest
        raise
    return token, context


def _clear_discovery_turn(context: ProjectContext, token: str) -> None:
    lease = context.generation_state.get("discovery_lease") or {}
    if isinstance(lease, dict) and lease.get("token") == token:
        context.generation_state.pop("discovery_lease", None)


async def _release_discovery_turn(
    context: ProjectContext, user_id: str, token: str
) -> None:
    """Best-effort CAS cleanup for quota, provider, timeout, and cancellation paths."""

    _clear_discovery_turn(context, token)
    try:
        await asyncio.shield(
            asyncio.to_thread(services.project_store.save, context, user_id)
        )
    except BaseException:  # noqa: BLE001 - preserve the original failure/cancel
        pass


def project_summary(context: ProjectContext) -> dict[str, Any]:
    return {
        "problem": context.problem,
        "target_users": context.target_users,
        "user_roles": context.user_roles,
        "business_goals": context.business_goals,
        "core_features": context.core_features,
        "scope": context.scope,
        "constraints": context.constraints,
        "assumptions": context.assumptions,
        "integrations": context.integrations,
        "security_requirements": context.security_requirements,
        "performance_requirements": context.performance_requirements,
        "deployment_requirements": context.deployment_requirements,
        "technology_preferences": context.technology_preferences,
    }


def project_response(
    context: ProjectContext, discovery: dict[str, Any] | None = None
) -> dict[str, Any]:
    if discovery is None:
        stored_discovery = context.generation_state.get("discovery_snapshot")
        discovery = stored_discovery if isinstance(stored_discovery, dict) else None
    return {
        "project_id": context.project_id,
        "title": " ".join(context.business_idea.strip().split())[:80]
        or "Untitled project",
        "status": context.status,
        "business_idea": context.business_idea,
        "summary": project_summary(context),
        "known_information": known_info_snapshot(context),
        "transcript": [turn.model_dump(mode="json") for turn in context.transcript],
        "discovery": discovery,
        "blueprint": {
            "requirements": context.requirements,
            "architecture": context.architecture,
            "database": context.database,
            "api": context.api,
            "devops": context.devops,
            "review": context.review,
        },
        "generation": services.orchestrator.generation_snapshot(context),
        "updated_at": context.updated_at.isoformat(),
    }


def _structured_output_for_file(
    context: ProjectContext, name: str
) -> dict[str, Any] | None:
    if name == "requirements.md":
        return context.requirements
    if name.startswith("architecture"):
        return context.architecture
    if name in {"database.md", "database.sql", "erd.mmd"}:
        return context.database
    if name in {"api.md", "openapi.yaml"}:
        return context.api
    if name in {
        "devops.md",
        "Dockerfile",
        "docker-compose.yml",
        "github-actions.yml",
    }:
        return context.devops
    if name == "overview.md":
        return project_summary(context)
    return None


async def _persist_artifacts(context: ProjectContext) -> list[str]:
    files = render_all(context)
    await asyncio.gather(
        *[
            asyncio.to_thread(
                services.artifact_store.write,
                context.project_id,
                name,
                content,
                _structured_output_for_file(context, name),
            )
            for name, content in files.items()
        ]
    )
    return sorted(files)


async def _discovery_turn(
    context: ProjectContext,
    message: str,
    user: CurrentUser,
    *,
    idempotency_key: str | None = None,
    operation: str = "discovery.message",
    fingerprint: str | None = None,
) -> dict[str, Any]:
    idempotency_key = idempotency_key or str(uuid.uuid4())
    fingerprint = fingerprint or operation_fingerprint(
        operation, context.project_id, message
    )
    receipt = _completed_request(
        context, idempotency_key, operation, fingerprint
    )
    if receipt is not None:
        # Repair the read-optimized message projection if the previous request
        # committed its context but lost the HTTP response before syncing it.
        await _sync_transcript_projection(context, user.id)
        return project_response(context)

    discovery_token, context = await _claim_discovery_turn(
        context, user.id, idempotency_key
    )
    try:
        await _claim_quota(
            user,
            "discovery",
            idempotency_key,
            context.project_id,
            fingerprint,
        )
        async with asyncio.timeout(
            services.settings.effective_request_deadline_s
        ):
            output = await services.orchestrator.discovery_turn(context, message)
    except TimeoutError as exc:
        # Preserve the user's turn so retrying does not silently lose their
        # message, while returning before the hosting request is terminated.
        await _release_discovery_turn(context, user.id, discovery_token)
        raise HTTPException(
            status_code=504,
            detail="The discovery agent reached its time limit. Please retry.",
        ) from exc
    except DiscoveryError as exc:
        await _release_discovery_turn(context, user.id, discovery_token)
        raise HTTPException(
            status_code=502,
            detail="The discovery agent could not complete this turn. Please retry.",
        ) from exc
    except BaseException:
        await _release_discovery_turn(context, user.id, discovery_token)
        raise
    _clear_discovery_turn(context, discovery_token)
    discovery_result = output.model_dump(mode="json")
    # Questions and options are part of the user-facing project state, not raw
    # provider telemetry. Persist the validated structured snapshot so opening
    # a project or replaying a committed request still renders the guided UI.
    context.generation_state["discovery_snapshot"] = discovery_result
    remember_operation(
        context,
        idempotency_key,
        operation,
        fingerprint,
    )
    await _save(context, user.id)
    return project_response(context, discovery_result)


@app.get("/api/health")
async def health():
    return {
        "status": "ok",
        "provider": services.settings.effective_provider(),
        "model": services.settings.effective_model(),
        "storage": "supabase" if services.settings.supabase_configured else "local",
        "auth_enabled": services.settings.auth_enabled,
    }


@app.get("/api/config")
async def public_config():
    """Return browser-safe configuration only (never the service-role key)."""

    return {
        "auth_enabled": services.settings.auth_enabled,
        "supabase_url": (
            services.settings.supabase_url if services.settings.auth_enabled else ""
        ),
        "supabase_anon_key": (
            services.settings.supabase_anon_key if services.settings.auth_enabled else ""
        ),
    }


@app.get("/api/projects")
async def list_projects(user: CurrentUser = Depends(current_user)):
    projects = await asyncio.to_thread(services.project_store.list_projects, user.id)
    return {"projects": projects}


@app.post("/api/projects", status_code=201)
async def create_project(
    request: CreateProjectRequest,
    user: CurrentUser = Depends(current_user),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    key = _request_idempotency_key(idempotency_key)
    idea = request.business_idea.strip()
    operation = "project.create"
    fingerprint = operation_fingerprint(operation, idea)
    project_id = deterministic_project_id(user.id, key)

    context = await asyncio.to_thread(
        services.project_store.load, project_id, user.id
    )
    if context is None:
        await _claim_quota(
            user, "project_create", key, project_id, fingerprint
        )
        try:
            context = await asyncio.to_thread(
                services.project_store.create, idea, project_id, user.id
            )
        except SupabasePersistenceError:
            # A successful insert can outlive a lost PostgREST response. The
            # deterministic ID lets this request reconcile that exact project.
            context = await asyncio.to_thread(
                services.project_store.load, project_id, user.id
            )
            if context is None:
                raise
    if context.business_idea.strip() != idea:
        raise HTTPException(
            status_code=409,
            detail="That request identifier was already used for another project.",
        )
    return await _discovery_turn(
        context,
        idea,
        user,
        idempotency_key=key,
        operation=operation,
        fingerprint=fingerprint,
    )


@app.post("/api/projects/{project_id}/discovery/start")
async def discovery_start(
    project_id: str,
    request: MessageRequest,
    user: CurrentUser = Depends(current_user),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    context = await _load(project_id, user.id)
    key = _request_idempotency_key(idempotency_key)
    operation = "discovery.start"
    fingerprint = operation_fingerprint(operation, project_id, request.message)
    return await _discovery_turn(
        context,
        request.message,
        user,
        idempotency_key=key,
        operation=operation,
        fingerprint=fingerprint,
    )


@app.post("/api/projects/{project_id}/discovery/message")
async def discovery_message(
    project_id: str,
    request: MessageRequest,
    user: CurrentUser = Depends(current_user),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    context = await _load(project_id, user.id)
    key = _request_idempotency_key(idempotency_key)
    operation = "discovery.message"
    fingerprint = operation_fingerprint(operation, project_id, request.message)
    return await _discovery_turn(
        context,
        request.message,
        user,
        idempotency_key=key,
        operation=operation,
        fingerprint=fingerprint,
    )


@app.get("/api/projects/{project_id}/discovery/state")
async def discovery_state(
    project_id: str, user: CurrentUser = Depends(current_user)
):
    return project_response(await _load(project_id, user.id))


@app.post("/api/projects/{project_id}/discovery/confirm")
async def discovery_confirm(
    project_id: str, user: CurrentUser = Depends(current_user)
):
    context = await _load(project_id, user.id)
    # Natural idempotency: confirmation may already have committed even when
    # the browser never received the response. Later workflow states also prove
    # that confirmation completed, and must never be moved backwards.
    if context.status in _CONFIRMATION_COMPLETE_STATUSES:
        return project_response(context)
    marker = context.generation_state.get("discovery_lease")
    if marker is not None:
        expiry = discovery_lease_expiry(context)
        token = str(marker.get("token") or "") if isinstance(marker, dict) else ""
        if (
            expiry is None
            or expiry > datetime.now(timezone.utc)
            or not token
        ):
            raise HTTPException(
                status_code=409,
                detail="A discovery response is still running. Please wait and retry.",
            )
        # Remove an expired receipt as its own optimistic write. If a new turn
        # wins the race before confirmation, the following reload/save loses
        # safely instead of confirming over live Gemini work.
        await _release_discovery_turn(context, user.id, token)
        context = await _load(project_id, user.id)
        if "discovery_lease" in context.generation_state:
            raise SupabaseConflictError("Discovery lease cleanup did not commit")
    try:
        services.orchestrator.confirm(context)
    except OrchestrationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    try:
        await _save(context, user.id)
    except SupabaseConflictError:
        # Reconcile two confirmation requests that loaded the same optimistic
        # version. If the competing request confirmed first, return its durable
        # state instead of making the user retry a completed operation.
        latest = await _load(project_id, user.id)
        if latest.status in _CONFIRMATION_COMPLETE_STATUSES:
            return project_response(latest)
        raise
    return project_response(context)


@app.post("/api/projects/{project_id}/generate")
async def start_generation(
    project_id: str, user: CurrentUser = Depends(current_user)
):
    """Initialize a durable workflow; no fragile background task is spawned."""

    context = await _load(project_id, user.id)
    try:
        services.orchestrator.initialize_generation(context)
    except OrchestrationError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc

    if services.workflow_store is not None:
        run_id = await asyncio.to_thread(
            services.workflow_store.start, project_id, user.id
        )
        context.generation_state["workflow_run_id"] = run_id
    await _save(context, user.id)
    response = project_response(context)
    response["complete"] = False
    return response


@app.post("/api/projects/{project_id}/generation/next")
async def generation_next(
    project_id: str,
    user: CurrentUser = Depends(current_user),
    idempotency_key: str | None = Header(default=None, alias="Idempotency-Key"),
):
    """Advance one checkpointed stage, persist it, and return current progress."""

    key = _request_idempotency_key(idempotency_key)
    operation = "generation.next"
    context = await _load(project_id, user.id)

    # A completed request advances ``next_stage``, so its canonical fingerprint
    # must be reconstructed from the immutable stage recorded in its receipt.
    # This preserves replay while ensuring a live quota claim is stage-bound.
    stored_receipt = operation_receipt(context, key)
    if stored_receipt is not None:
        stored_result = stored_receipt.get("result")
        completed_stage = (
            str(stored_result.get("stage") or "")
            if isinstance(stored_result, dict)
            else ""
        )
        if not completed_stage:
            raise HTTPException(
                status_code=409,
                detail="That request identifier was already used for another operation.",
            )
        replay_fingerprint = operation_fingerprint(
            operation, project_id, completed_stage
        )
        receipt = _completed_request(
            context, key, operation, replay_fingerprint
        )
        artifact_names = await _persist_artifacts(context)
        response = project_response(context)
        response.update(receipt)
        response["artifacts"] = artifact_names
        return response

    if context.status in {"approved", "revised", "needs_attention"}:
        artifact_names = await _persist_artifacts(context)
        response = project_response(context)
        response.update({"complete": True, "artifacts": artifact_names})
        return response

    if context.status != "generating":
        raise HTTPException(
            status_code=409,
            detail="Start generation before advancing an agent stage.",
        )

    run_id = str(context.generation_state.get("workflow_run_id") or "")
    stage = str(context.generation_state.get("next_stage") or "")
    if not stage:
        raise HTTPException(
            status_code=409,
            detail="The generation checkpoint has no next stage.",
        )
    fingerprint = operation_fingerprint(operation, project_id, stage)
    lease_token = ""

    if hasattr(services.project_store, "claim_generation_stage"):
        lease_token = await asyncio.to_thread(
            services.project_store.claim_generation_stage,
            context,
            user.id,
            stage,
        )

    # A failed attempt records durable diagnostics so the UI can explain why
    # the stage stopped. Once a retry owns the stage, those diagnostics are no
    # longer current and must not survive the successful checkpoint.
    context.generation_state.pop("last_stage_error", None)
    context.generation_state.pop("last_stage_error_at", None)
    context.generation_state["last_error"] = None

    try:
        await _claim_quota(
            user, "generation_stage", key, project_id, fingerprint
        )
    except asyncio.CancelledError:
        await _shielded_release_generation_lease(
            context.project_id,
            user.id,
            lease_token,
            run_id,
            stage,
            "request_cancelled",
        )
        raise
    except SupabaseQuotaError:
        await _release_generation_lease(
            context.project_id,
            user.id,
            lease_token,
            run_id,
            stage,
            "daily_quota_exceeded",
        )
        raise
    except SupabasePersistenceError:
        await _release_generation_lease(
            context.project_id,
            user.id,
            lease_token,
            run_id,
            stage,
            "quota_check_failed",
        )
        raise

    try:
        if services.workflow_store is not None and run_id:
            await asyncio.to_thread(
                services.workflow_store.update,
                run_id,
                status="running",
                current_stage=stage,
            )
        async with asyncio.timeout(
            services.settings.effective_request_deadline_s
        ):
            progress = await services.orchestrator.generate_next(context)
    except asyncio.CancelledError:
        # Cooperative request cancellation is distinct from a provider timeout.
        # Run recovery in its own shielded task so cancellation cannot strand a
        # durable stage lease until its TTL expires, then preserve cancellation
        # semantics for the ASGI host.
        await _shielded_release_generation_lease(
            context.project_id,
            user.id,
            lease_token,
            run_id,
            stage,
            "request_cancelled",
        )
        raise
    except TimeoutError as exc:
        await _release_generation_lease(
            context.project_id,
            user.id,
            lease_token,
            run_id,
            stage,
            "stage_timeout",
        )
        raise HTTPException(
            status_code=504,
            detail="The generation stage reached its time limit. Please retry it.",
        ) from exc
    except OrchestrationError as exc:
        await _release_generation_lease(
            context.project_id,
            user.id,
            lease_token,
            run_id,
            stage,
            "invalid_stage_transition",
        )
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    except Exception as exc:  # noqa: BLE001 - release durable claim on any fault
        await _release_generation_lease(
            context.project_id,
            user.id,
            lease_token,
            run_id,
            stage,
            type(exc).__name__,
        )
        if isinstance(exc, SupabasePersistenceError):
            raise
        raise HTTPException(
            status_code=502,
            detail="The generation stage failed safely. Please retry it.",
        ) from exc

    receipt_result = {
        "stage": progress.get("stage"),
        "completed_now": progress.get("completed_now", []),
        "next_stage": progress.get("next_stage"),
        "complete": bool(progress["complete"]),
    }
    remember_operation(
        context,
        key,
        operation,
        fingerprint,
        receipt_result,
    )

    if lease_token:
        try:
            await asyncio.to_thread(
                services.project_store.commit_generation_stage,
                context,
                user.id,
                stage,
                lease_token,
                workflow_run_id=run_id or None,
                workflow_summary=progress,
            )
        except asyncio.CancelledError:
            # The synchronous PostgREST request may still win after the await is
            # cancelled. Releasing the same token is safe in both outcomes: it
            # is a no-op after a successful commit and unlocks the stage if the
            # commit did not happen. Do not rewrite workflow status here because
            # the atomic SQL commit may already have completed it.
            await _shielded_release_generation_project_lease(
                context.project_id,
                user.id,
                lease_token,
                stage,
                "checkpoint_commit_cancelled",
            )
            raise
        except SupabasePersistenceError:
            # If the response was lost after a successful commit, this release
            # is a harmless no-op because the token has already been cleared.
            # If the commit never happened, it immediately makes the same stage
            # retryable rather than forcing the user to wait for lease expiry.
            try:
                await asyncio.to_thread(
                    services.project_store.release_generation_stage,
                    context.project_id,
                    user.id,
                    lease_token,
                    {"reason": "checkpoint_commit_uncertain", "stage": stage},
                )
            except SupabasePersistenceError:
                pass
            raise
    else:
        # Local development keeps the original store contract.
        await _save(context, user.id)

    # Artifact writes are idempotent and happen after the canonical checkpoint.
    # A retry therefore resumes at the next stage instead of paying Gemini for
    # the completed stage again.
    artifact_names = await _persist_artifacts(context)

    if services.workflow_store is not None and run_id and not lease_token:
        workflow_status = context.status if progress["complete"] else "running"
        await asyncio.to_thread(
            services.workflow_store.update,
            run_id,
            status=workflow_status,
            current_stage=str(progress.get("next_stage") or "complete"),
            summary=progress,
        )

    response = project_response(context)
    response.update(receipt_result)
    response["artifacts"] = artifact_names
    return response


async def _release_generation_lease(
    project_id: str,
    user_id: str,
    lease_token: str,
    workflow_run_id: str,
    stage: str,
    reason: str,
) -> None:
    """Best-effort recovery that keeps the same stage safely retryable."""

    if lease_token and hasattr(services.project_store, "release_generation_stage"):
        try:
            await asyncio.to_thread(
                services.project_store.release_generation_stage,
                project_id,
                user_id,
                lease_token,
                {"reason": reason, "stage": stage},
            )
        except SupabasePersistenceError:
            pass
    if services.workflow_store is not None and workflow_run_id:
        try:
            await asyncio.to_thread(
                services.workflow_store.update,
                workflow_run_id,
                status="queued",
                current_stage=stage,
                error={"reason": reason, "stage": stage},
            )
        except SupabasePersistenceError:
            pass


async def _shielded_release_generation_lease(
    project_id: str,
    user_id: str,
    lease_token: str,
    workflow_run_id: str,
    stage: str,
    reason: str,
) -> None:
    """Run cancellation recovery in a task insulated from caller cancellation."""

    try:
        await asyncio.shield(
            _release_generation_lease(
                project_id,
                user_id,
                lease_token,
                workflow_run_id,
                stage,
                reason,
            )
        )
    except BaseException:  # noqa: BLE001 - cleanup is strictly best-effort
        pass


async def _shielded_release_generation_project_lease(
    project_id: str,
    user_id: str,
    lease_token: str,
    stage: str,
    reason: str,
) -> None:
    """Release only the project lease when commit completion is uncertain."""

    if not lease_token or not hasattr(
        services.project_store, "release_generation_stage"
    ):
        return
    try:
        await asyncio.shield(
            asyncio.to_thread(
                services.project_store.release_generation_stage,
                project_id,
                user_id,
                lease_token,
                {"reason": reason, "stage": stage},
            )
        )
    except BaseException:  # noqa: BLE001 - cleanup is strictly best-effort
        pass


@app.get("/api/projects/{project_id}/generation/status")
async def generation_status(
    project_id: str, user: CurrentUser = Depends(current_user)
):
    """Pollable durable status (replaces the old instance-local SSE buffer)."""

    context = await _load(project_id, user.id)
    return services.orchestrator.generation_snapshot(context)


@app.get("/api/projects/{project_id}")
async def get_project(project_id: str, user: CurrentUser = Depends(current_user)):
    return project_response(await _load(project_id, user.id))


@app.get("/api/projects/{project_id}/outputs")
async def get_outputs(project_id: str, user: CurrentUser = Depends(current_user)):
    context = await _load(project_id, user.id)
    return {
        "project_id": project_id,
        "blueprint": project_response(context)["blueprint"],
    }


@app.get("/api/projects/{project_id}/runs")
async def get_runs(project_id: str, user: CurrentUser = Depends(current_user)):
    await _load(project_id, user.id)
    records = await asyncio.to_thread(services.tracker.list, project_id)
    return {
        "project_id": project_id,
        "runs": [
            {
                "agent": record.agent,
                "status": record.status,
                "started_at": record.started_at.isoformat(),
                "completed_at": (
                    record.completed_at.isoformat() if record.completed_at else None
                ),
                "duration_ms": record.duration_ms,
                "retry_count": record.retry_count,
                "provider": record.provider,
                "model": record.model,
                "input_tokens": record.input_tokens,
                "output_tokens": record.output_tokens,
                "total_tokens": record.total_tokens,
                "error": record.error,
            }
            for record in records
            if record.status != "started"
        ],
    }


@app.get("/api/projects/{project_id}/artifacts")
async def list_artifacts(
    project_id: str, user: CurrentUser = Depends(current_user)
):
    await _load(project_id, user.id)
    if hasattr(services.artifact_store, "list_metadata"):
        artifacts = await asyncio.to_thread(
            services.artifact_store.list_metadata, project_id
        )
    else:
        names = await asyncio.to_thread(services.artifact_store.list, project_id)
        artifacts = [{"name": name} for name in names]
    return {"project_id": project_id, "artifacts": artifacts}


@app.get(
    "/api/projects/{project_id}/artifacts/{artifact_type:path}",
    response_class=PlainTextResponse,
)
async def get_artifact(
    project_id: str,
    artifact_type: str,
    user: CurrentUser = Depends(current_user),
):
    await _load(project_id, user.id)
    content = await asyncio.to_thread(
        services.artifact_store.read, project_id, artifact_type
    )
    if content is None:
        raise HTTPException(status_code=404, detail="Artifact not found")
    download_name = "".join(
        char
        for char in artifact_type.replace("\\", "/").split("/")[-1]
        if char.isalnum() or char in {".", "-", "_"}
    ) or "artifact.txt"
    return PlainTextResponse(
        content,
        headers={
            "Content-Disposition": f'inline; filename="{download_name}"',
            "X-Content-Type-Options": "nosniff",
        },
    )


@app.delete("/api/projects/{project_id}", status_code=204)
async def delete_project(
    project_id: str, user: CurrentUser = Depends(current_user)
):
    await _load(project_id, user.id)
    await asyncio.to_thread(services.project_store.delete, project_id, user.id)
    return None


if __name__ == "__main__":  # pragma: no cover - convenience only
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=8000)
