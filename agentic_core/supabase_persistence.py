"""Durable Supabase persistence adapters used by the production API.

The agent core deliberately keeps its small local SQLite/JSONL/filesystem
stores for tests and offline development.  These adapters implement the same
public contracts over Supabase's PostgREST API so Vercel functions never rely
on an instance-local disk or in-memory job state.

Only the backend imports this module.  The service-role credential is kept in
the HTTP client's private headers and is never returned or logged.
"""

from __future__ import annotations

import hashlib
import mimetypes
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import httpx

from .orchestrator.tracker import RunRecord
from .schemas import ProjectContext


_SENSITIVE_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "password",
    "secret",
    "service_role_key",
    "token",
}


class SupabasePersistenceError(RuntimeError):
    """A safe, credential-free error raised by a Supabase adapter."""


class SupabaseAuthenticationError(RuntimeError):
    """The supplied browser access token was missing, expired, or invalid."""


class SupabaseConflictError(SupabasePersistenceError):
    """An optimistic lock or generation-stage lease could not be acquired."""


class SupabaseIdempotencyConflictError(SupabaseConflictError):
    """One client operation key was reused with different request semantics."""


class SupabaseQuotaError(SupabasePersistenceError):
    """The authenticated account exhausted a server-side daily allowance."""


def redact_secrets(value: Any) -> Any:
    """Recursively redact secret-looking fields before durable telemetry writes."""

    if isinstance(value, dict):
        cleaned: dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).lower().replace("-", "_")
            is_token_metric = normalized in {
                "input_tokens",
                "output_tokens",
                "total_tokens",
                "prompt_token_count",
                "candidates_token_count",
                "total_token_count",
            }
            if not is_token_metric and (
                normalized in _SENSITIVE_KEYS
                or any(
                    part in normalized
                    for part in ("password", "secret", "api_key", "access_token", "refresh_token")
                )
            ):
                cleaned[str(key)] = "[REDACTED]"
            else:
                cleaned[str(key)] = redact_secrets(item)
        return cleaned
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    if isinstance(value, tuple):
        return [redact_secrets(item) for item in value]
    return value


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def _rpc_integer(result: Any, key: str) -> int:
    """Normalize PostgREST scalar/table RPC responses to one integer."""

    value = result
    if isinstance(result, list):
        if not result:
            raise SupabasePersistenceError("Supabase RPC returned no result")
        value = result[0]
    if isinstance(value, dict):
        mapping = value
        value = mapping.get(key)
        if value is None and len(mapping) == 1:
            value = next(iter(mapping.values()))
    try:
        return int(value)
    except (TypeError, ValueError) as exc:
        raise SupabasePersistenceError("Supabase RPC returned an invalid version") from exc


class SupabaseGateway:
    """Small PostgREST/Auth client with redacted error reporting."""

    def __init__(self, url: str, anon_key: str, service_role_key: str):
        self.url = url.rstrip("/")
        self._anon_key = anon_key
        self._service_role_key = service_role_key
        self._client = httpx.Client(
            base_url=f"{self.url}/",
            timeout=httpx.Timeout(8.0),
            headers={
                "apikey": service_role_key,
                "Authorization": f"Bearer {service_role_key}",
                "Content-Type": "application/json",
            },
        )

    def close(self) -> None:
        self._client.close()

    def _request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, str] | None = None,
        payload: Any = None,
        prefer: str | None = None,
        headers: dict[str, str] | None = None,
    ) -> Any:
        request_headers = dict(headers or {})
        if prefer:
            request_headers["Prefer"] = prefer
        try:
            response = self._client.request(
                method,
                path.lstrip("/"),
                params=params,
                json=payload,
                headers=request_headers,
            )
        except httpx.HTTPError as exc:
            raise SupabasePersistenceError(
                f"Supabase request failed ({type(exc).__name__})"
            ) from exc
        if response.status_code >= 400:
            detail = ""
            try:
                body = redact_secrets(response.json())
                if isinstance(body, dict):
                    detail = str(body.get("message") or body.get("details") or "")[:300]
            except Exception:  # noqa: BLE001 - never leak an unparsed response body
                detail = ""
            suffix = f": {detail}" if detail else ""
            raise SupabasePersistenceError(
                f"Supabase request returned HTTP {response.status_code}{suffix}"
            )
        if response.status_code == 204 or not response.content:
            return None
        return response.json()

    def select(
        self,
        table: str,
        *,
        filters: dict[str, str] | None = None,
        columns: str = "*",
        order: str | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        params = {"select": columns, **(filters or {})}
        if order:
            params["order"] = order
        if limit is not None:
            params["limit"] = str(limit)
        result = self._request("GET", f"rest/v1/{table}", params=params)
        return result or []

    def insert(
        self,
        table: str,
        rows: dict[str, Any] | list[dict[str, Any]],
        *,
        upsert: bool = False,
        on_conflict: str | None = None,
        return_rows: bool = False,
    ) -> list[dict[str, Any]]:
        params = {"on_conflict": on_conflict} if on_conflict else None
        preference = []
        if upsert:
            preference.append("resolution=merge-duplicates")
        preference.append("return=representation" if return_rows else "return=minimal")
        result = self._request(
            "POST",
            f"rest/v1/{table}",
            params=params,
            # Canonical project, message, and artifact data must be stored
            # verbatim.  Telemetry callers explicitly redact their own payloads.
            payload=rows,
            prefer=",".join(preference),
        )
        return result or []

    def update(
        self,
        table: str,
        values: dict[str, Any],
        *,
        filters: dict[str, str],
        return_rows: bool = False,
    ) -> list[dict[str, Any]]:
        result = self._request(
            "PATCH",
            f"rest/v1/{table}",
            params=filters,
            payload=values,
            prefer="return=representation" if return_rows else "return=minimal",
        )
        return result or []

    def rpc(self, function: str, payload: dict[str, Any]) -> Any:
        try:
            return self._request(
                "POST",
                f"rest/v1/rpc/{function}",
                payload=payload,
            )
        except SupabasePersistenceError as exc:
            if "daily_quota_exceeded" in str(exc).lower():
                raise SupabaseQuotaError(str(exc)) from exc
            if "quota_idempotency_conflict" in str(exc).lower():
                raise SupabaseIdempotencyConflictError(str(exc)) from exc
            if "conflict" in str(exc).lower() or "lease" in str(exc).lower():
                raise SupabaseConflictError(str(exc)) from exc
            raise

    def delete(self, table: str, *, filters: dict[str, str]) -> None:
        self._request(
            "DELETE",
            f"rest/v1/{table}",
            params=filters,
            prefer="return=minimal",
        )

    def verify_user(self, access_token: str) -> dict[str, Any]:
        """Validate a Supabase access token without exposing it to logs/errors."""

        if not access_token:
            raise SupabaseAuthenticationError("Authentication is required")
        try:
            response = self._client.get(
                "auth/v1/user",
                headers={
                    "apikey": self._anon_key,
                    "Authorization": f"Bearer {access_token}",
                },
            )
        except httpx.HTTPError as exc:
            # A transport failure says nothing about the validity of the
            # browser's session. Surface it as an upstream availability fault
            # so the API returns 503 instead of incorrectly signing the user
            # out with a 401 response.
            raise SupabasePersistenceError(
                "Supabase Auth is temporarily unavailable"
            ) from exc
        if response.status_code in {401, 403}:
            raise SupabaseAuthenticationError("The sign-in session is invalid or expired")
        if response.status_code == 429 or response.status_code >= 500:
            raise SupabasePersistenceError("Supabase Auth is temporarily unavailable")
        if response.status_code != 200:
            raise SupabasePersistenceError("Supabase Auth returned an unexpected response")
        try:
            payload = response.json()
        except ValueError as exc:
            raise SupabasePersistenceError(
                "Supabase Auth returned an invalid response"
            ) from exc
        if not isinstance(payload, dict) or not payload.get("id"):
            # A 200 response without an identity is a malformed upstream
            # response, not proof that the caller supplied an invalid session.
            raise SupabasePersistenceError(
                "Supabase Auth returned an invalid response"
            )
        return payload


class SupabaseProjectStore:
    """Project contexts stored as JSONB, scoped to a verified Supabase user."""

    def __init__(self, gateway: SupabaseGateway):
        self._gateway = gateway

    def create(
        self,
        business_idea: str,
        project_id: str | None = None,
        owner_id: str | None = None,
    ) -> ProjectContext:
        if not owner_id:
            raise ValueError("owner_id is required for Supabase projects")
        project_id = project_id or f"proj_{uuid.uuid4().hex[:10]}"
        context = ProjectContext(project_id=project_id, business_idea=business_idea)
        title = " ".join(business_idea.strip().split())[:80] or "Untitled project"
        self._gateway.insert(
            "projects",
            {
                "id": project_id,
                "user_id": owner_id,
                "business_idea": business_idea,
                "title": title,
                "status": context.status,
                "context": context.model_dump(mode="json"),
                "generation_state": {},
                "context_version": 0,
                "updated_at": context.updated_at.isoformat(),
            },
        )
        return context

    def save(self, context: ProjectContext, owner_id: str | None = None) -> None:
        if not owner_id:
            raise ValueError("owner_id is required for Supabase project updates")
        result = self._gateway.rpc(
            "save_project_context",
            {
                "p_project_id": context.project_id,
                "p_user_id": owner_id,
                "p_expected_version": context.persistence_version,
                "p_business_idea": context.business_idea,
                "p_title": " ".join(context.business_idea.strip().split())[:80]
                or "Untitled project",
                "p_status": context.status,
                "p_context": context.model_dump(mode="json"),
                "p_generation_state": context.generation_state,
            },
        )
        context.persistence_version = _rpc_integer(result, "context_version")

    def load(
        self, project_id: str, owner_id: str | None = None
    ) -> ProjectContext | None:
        filters = {"id": f"eq.{project_id}"}
        if owner_id:
            filters["user_id"] = f"eq.{owner_id}"
        rows = self._gateway.select(
            "projects", filters=filters, columns="id,context,context_version", limit=1
        )
        if not rows:
            return None
        row = rows[0]
        context = ProjectContext.model_validate(row["context"])
        # Never trust a user-editable JSON identifier as the write target.  The
        # database also enforces this equality as a defence-in-depth CHECK.
        if context.project_id != project_id or str(row["id"]) != project_id:
            raise SupabasePersistenceError("Stored project identity is invalid")
        context.persistence_version = int(row.get("context_version") or 0)
        return context

    def claim_generation_stage(
        self,
        context: ProjectContext,
        owner_id: str,
        stage: str,
    ) -> str:
        lease_token = str(uuid.uuid4())
        payload = {
            "p_project_id": context.project_id,
            "p_user_id": owner_id,
            "p_expected_version": context.persistence_version,
            "p_expected_stage": stage,
            "p_lease_token": lease_token,
            "p_lease_seconds": 270,
        }
        try:
            result = self._gateway.rpc(
                "claim_generation_stage_idempotent", payload
            )
        except SupabaseConflictError:
            raise
        except SupabasePersistenceError:
            # Replaying the same caller-generated token reconciles the common
            # serverless uncertainty case: DB commit succeeded but its HTTP
            # response was lost. The SQL function returns the existing claim.
            result = self._gateway.rpc(
                "claim_generation_stage_idempotent", payload
            )
        row = result[0] if isinstance(result, list) and result else result
        if not isinstance(row, dict) or not row.get("lease_token"):
            raise SupabaseConflictError("Generation stage claim returned no lease")
        if str(row["lease_token"]) != lease_token:
            raise SupabaseConflictError("Generation stage returned a different lease")
        try:
            returned_version = int(row["context_version"])
            # The atomic claim itself increments the optimistic-lock version.
            # The commit must use that returned version with the lease token.
            if returned_version != context.persistence_version + 1:
                raise ValueError("unexpected claimed version")
        except (KeyError, TypeError, ValueError) as exc:
            # We have a real lease token, so do not strand the stage if a stale
            # or incompatible SQL function returned an unexpected shape.
            try:
                self._gateway.rpc(
                    "release_generation_stage",
                    {
                        "p_project_id": context.project_id,
                        "p_user_id": owner_id,
                        "p_lease_token": lease_token,
                        "p_error": {"reason": "invalid_claim_response"},
                    },
                )
            except SupabasePersistenceError:
                pass
            raise SupabaseConflictError(
                "Generation stage returned an invalid version"
            ) from exc
        context.persistence_version = returned_version
        return lease_token

    def commit_generation_stage(
        self,
        context: ProjectContext,
        owner_id: str,
        stage: str,
        lease_token: str,
        *,
        workflow_run_id: str | None = None,
        workflow_summary: dict[str, Any] | None = None,
    ) -> None:
        result = self._gateway.rpc(
            "commit_generation_stage",
            {
                "p_project_id": context.project_id,
                "p_user_id": owner_id,
                "p_expected_version": context.persistence_version,
                "p_expected_stage": stage,
                "p_lease_token": lease_token,
                "p_business_idea": context.business_idea,
                "p_title": " ".join(context.business_idea.strip().split())[:80]
                or "Untitled project",
                "p_status": context.status,
                "p_context": context.model_dump(mode="json"),
                "p_generation_state": context.generation_state,
                "p_workflow_run_id": workflow_run_id or None,
                "p_workflow_summary": redact_secrets(workflow_summary or {}),
            },
        )
        context.persistence_version = _rpc_integer(result, "context_version")

    def release_generation_stage(
        self,
        project_id: str,
        owner_id: str,
        lease_token: str,
        error: dict[str, Any] | None = None,
    ) -> None:
        self._gateway.rpc(
            "release_generation_stage",
            {
                "p_project_id": project_id,
                "p_user_id": owner_id,
                "p_lease_token": lease_token,
                "p_error": redact_secrets(error or {}),
            },
        )

    def list_ids(self, owner_id: str | None = None) -> list[str]:
        filters = {"user_id": f"eq.{owner_id}"} if owner_id else None
        rows = self._gateway.select(
            "projects", filters=filters, columns="id", order="updated_at.desc"
        )
        return [str(row["id"]) for row in rows]

    def list_projects(self, owner_id: str) -> list[dict[str, Any]]:
        rows = self._gateway.select(
            "projects",
            filters={"user_id": f"eq.{owner_id}"},
            columns="id,title,business_idea,status,created_at,updated_at",
            order="updated_at.desc",
        )
        return [
            {
                "project_id": row["id"],
                "title": row.get("title") or row.get("business_idea") or "Untitled project",
                "business_idea": row.get("business_idea") or "",
                "status": row.get("status") or "discovery",
                "created_at": row.get("created_at"),
                "updated_at": row.get("updated_at"),
            }
            for row in rows
        ]

    def delete(self, project_id: str, owner_id: str) -> None:
        self._gateway.delete(
            "projects",
            filters={"id": f"eq.{project_id}", "user_id": f"eq.{owner_id}"},
        )

    def owner_for(self, project_id: str) -> str | None:
        rows = self._gateway.select(
            "projects",
            filters={"id": f"eq.{project_id}"},
            columns="user_id",
            limit=1,
        )
        return str(rows[0]["user_id"]) if rows else None

    def sync_transcript(self, context: ProjectContext, owner_id: str) -> None:
        conversations = self._gateway.select(
            "conversations",
            filters={
                "project_id": f"eq.{context.project_id}",
                "user_id": f"eq.{owner_id}",
                "is_default": "eq.true",
            },
            columns="id",
            limit=1,
        )
        if conversations:
            conversation_id = conversations[0]["id"]
        else:
            created = self._gateway.insert(
                "conversations",
                {
                    "project_id": context.project_id,
                    "user_id": owner_id,
                    "title": " ".join(context.business_idea.strip().split())[:80]
                    or "Project conversation",
                    "is_default": True,
                },
                return_rows=True,
            )
            conversation_id = created[0]["id"]

        rows: list[dict[str, Any]] = []
        for index, turn in enumerate(context.transcript):
            is_user = turn.role == "user"
            rows.append(
                {
                    "conversation_id": conversation_id,
                    "project_id": context.project_id,
                    "sender_user_id": owner_id if is_user else None,
                    "role": "user" if is_user else "assistant",
                    "content": turn.message,
                    "turn_index": index,
                    "created_at": turn.timestamp.isoformat(),
                }
            )
        if rows:
            self._gateway.insert(
                "messages",
                rows,
                upsert=True,
                on_conflict="conversation_id,turn_index",
            )


class SupabaseArtifactStore:
    """Rendered text artifacts persisted in a project-owned Supabase table."""

    def __init__(self, gateway: SupabaseGateway):
        self._gateway = gateway

    @staticmethod
    def _content_type(name: str) -> str:
        lower = name.lower()
        if lower == "dockerfile":
            return "text/x-dockerfile"
        if lower.endswith((".yaml", ".yml")):
            return "application/yaml"
        if lower.endswith(".md"):
            return "text/markdown"
        if lower.endswith(".mmd"):
            return "text/vnd.mermaid"
        return mimetypes.guess_type(name)[0] or "text/plain"

    def write(
        self,
        project_id: str,
        name: str,
        content: str,
        structured_data: dict[str, Any] | None = None,
    ) -> Path:
        encoded = content.encode("utf-8")
        self._gateway.insert(
            "artifacts",
            {
                "project_id": project_id,
                "name": name,
                "mime_type": self._content_type(name),
                "content_text": content,
                "structured_data": structured_data or {},
                "byte_size": len(encoded),
                "sha256": hashlib.sha256(encoded).hexdigest(),
                "updated_at": _utcnow(),
            },
            upsert=True,
            on_conflict="project_id,name",
        )
        return Path(name)

    def list(self, project_id: str) -> list[str]:
        rows = self._gateway.select(
            "artifacts",
            filters={"project_id": f"eq.{project_id}"},
            columns="name",
            order="name.asc",
        )
        return [str(row["name"]) for row in rows]

    def list_metadata(self, project_id: str) -> list[dict[str, Any]]:
        return self._gateway.select(
            "artifacts",
            filters={"project_id": f"eq.{project_id}"},
            columns="name,mime_type,byte_size,sha256,created_at,updated_at",
            order="name.asc",
        )

    def read(self, project_id: str, name: str) -> str | None:
        rows = self._gateway.select(
            "artifacts",
            filters={"project_id": f"eq.{project_id}", "name": f"eq.{name}"},
            columns="content_text",
            limit=1,
        )
        return str(rows[0].get("content_text") or "") if rows else None


class SupabaseExecutionTracker:
    """ExecutionTracker-compatible agent telemetry persisted as table rows."""

    def __init__(self, gateway: SupabaseGateway):
        self._gateway = gateway

    def start(
        self, agent: str, project_id: str, input_: dict[str, Any] | None
    ) -> RunRecord:
        active = self._gateway.select(
            "workflow_runs",
            filters={
                "project_id": f"eq.{project_id}",
                "status": "in.(queued,running)",
            },
            columns="id",
            order="created_at.desc",
            limit=1,
        )
        record = RunRecord(
            record_id=str(uuid.uuid4()),
            workflow_run_id=str(active[0]["id"]) if active else None,
            project_id=project_id,
            agent=agent,
            status="started",
            input=redact_secrets(input_),
            started_at=datetime.now(timezone.utc),
        )
        self._gateway.insert(
            "agent_runs",
            {
                "id": record.record_id,
                "workflow_run_id": record.workflow_run_id,
                "project_id": project_id,
                "agent": agent,
                "status": "started",
                "input_data": record.input,
                "started_at": record.started_at.isoformat(),
            },
        )
        return record

    def complete(self, record: RunRecord, result) -> None:
        record.status = result.status
        record.output = redact_secrets(result.output)
        record.error = str(result.error)[:2000] if result.error else None
        record.duration_ms = result.duration_ms
        record.retry_count = result.retry_count
        record.input_chars = result.input_chars
        record.output_chars = result.output_chars
        record.call_id = result.call_id
        record.provider = result.provider
        record.model = result.model
        record.ttft_s = result.ttft_s
        record.total_s = result.total_s
        record.input_tokens = result.input_tokens
        record.output_tokens = result.output_tokens
        record.total_tokens = result.total_tokens
        record.completed_at = datetime.now(timezone.utc)
        self._gateway.update(
            "agent_runs",
            {
                "status": record.status,
                "output_data": record.output,
                "error": record.error,
                "completed_at": record.completed_at.isoformat(),
                "duration_ms": record.duration_ms,
                "retry_count": record.retry_count,
                "input_chars": record.input_chars,
                "output_chars": record.output_chars,
                "external_call_id": record.call_id,
                "provider": record.provider,
                "model": record.model,
                "telemetry": {
                    "ttft_s": record.ttft_s,
                    "total_s": record.total_s,
                    "input_tokens": record.input_tokens,
                    "output_tokens": record.output_tokens,
                    "total_tokens": record.total_tokens,
                },
            },
            filters={"id": f"eq.{record.record_id}"},
        )
        # Store one sanitized provider/API telemetry row per live agent call.
        # Raw prompts and model outputs remain in the project/agent records and
        # are intentionally not duplicated here.
        self._gateway.insert(
            "api_calls",
            {
                "project_id": record.project_id,
                "workflow_run_id": record.workflow_run_id,
                "agent_run_id": record.record_id,
                "provider": record.provider or "unknown",
                "operation": f"generate_content:{record.agent}",
                "status": "success" if record.status == "success" else "failed",
                "external_call_id": record.call_id or None,
                "request_metadata": redact_secrets(
                    {
                        "agent": record.agent,
                        "input_chars": record.input_chars,
                        "retry_count": record.retry_count,
                    }
                ),
                "response_metadata": {
                    "output_chars": record.output_chars,
                    "model": record.model,
                },
                "telemetry": {
                    "ttft_s": record.ttft_s,
                    "total_s": record.total_s,
                    "input_tokens": record.input_tokens,
                    "output_tokens": record.output_tokens,
                    "total_tokens": record.total_tokens,
                },
                "error": redact_secrets(
                    {"message": record.error} if record.error else {}
                ),
                "sanitized": True,
                "started_at": record.started_at.isoformat(),
                "completed_at": record.completed_at.isoformat(),
                "duration_ms": record.duration_ms,
            },
        )

    def list(self, project_id: str) -> list[RunRecord]:
        rows = self._gateway.select(
            "agent_runs",
            filters={"project_id": f"eq.{project_id}"},
            order="started_at.asc",
        )
        records: list[RunRecord] = []
        for row in rows:
            telemetry = row.get("telemetry") or {}
            records.append(
                RunRecord(
                    record_id=str(row.get("id") or ""),
                    workflow_run_id=(
                        str(row["workflow_run_id"])
                        if row.get("workflow_run_id")
                        else None
                    ),
                    project_id=str(row["project_id"]),
                    agent=str(row["agent"]),
                    status=str(row.get("status") or "started"),
                    input=row.get("input_data"),
                    output=row.get("output_data"),
                    error=row.get("error"),
                    started_at=row.get("started_at") or datetime.now(timezone.utc),
                    completed_at=row.get("completed_at"),
                    duration_ms=row.get("duration_ms"),
                    retry_count=row.get("retry_count") or 0,
                    input_chars=row.get("input_chars") or 0,
                    output_chars=row.get("output_chars") or 0,
                    call_id=row.get("external_call_id") or "",
                    provider=row.get("provider") or "",
                    model=row.get("model") or "",
                    ttft_s=telemetry.get("ttft_s") or 0.0,
                    total_s=telemetry.get("total_s") or 0.0,
                    input_tokens=telemetry.get("input_tokens") or 0,
                    output_tokens=telemetry.get("output_tokens") or 0,
                    total_tokens=telemetry.get("total_tokens") or 0,
                )
            )
        return records


class SupabaseWorkflowStore:
    """Durable lifecycle rows for the staged Vercel workflow."""

    def __init__(self, gateway: SupabaseGateway):
        self._gateway = gateway

    def find_active(self, project_id: str) -> dict[str, Any] | None:
        rows = self._gateway.select(
            "workflow_runs",
            filters={
                "project_id": f"eq.{project_id}",
                "status": "in.(queued,running)",
            },
            order="created_at.desc",
            limit=1,
        )
        return rows[0] if rows else None

    def start(self, project_id: str, user_id: str) -> str:
        active = self.find_active(project_id)
        if active:
            return str(active["id"])
        run_id = str(uuid.uuid4())
        try:
            self._gateway.insert(
                "workflow_runs",
                {
                    "id": run_id,
                    "project_id": project_id,
                    "user_id": user_id,
                    "status": "queued",
                    "current_stage": "requirements",
                },
            )
        except SupabasePersistenceError:
            # A concurrent request may have won the partial-unique-index race.
            # Refetch the canonical active run instead of surfacing a 503.
            active = self.find_active(project_id)
            if active:
                return str(active["id"])
            raise
        return run_id

    def update(
        self,
        run_id: str,
        *,
        status: str,
        current_stage: str | None = None,
        summary: dict[str, Any] | None = None,
        error: dict[str, Any] | None = None,
    ) -> None:
        values: dict[str, Any] = {"status": status}
        if current_stage is not None:
            values["current_stage"] = current_stage
        if summary is not None:
            values["summary"] = redact_secrets(summary)
        if error is not None:
            values["error"] = redact_secrets(error)
        if status == "running":
            self._gateway.update(
                "workflow_runs",
                {"started_at": _utcnow()},
                filters={"id": f"eq.{run_id}", "started_at": "is.null"},
            )
        if status in {
            "approved",
            "revised",
            "needs_attention",
            "failed",
            "cancelled",
        }:
            values["completed_at"] = _utcnow()
        self._gateway.update(
            "workflow_runs", values, filters={"id": f"eq.{run_id}"}
        )
