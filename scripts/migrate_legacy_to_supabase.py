#!/usr/bin/env python3
"""Safely migrate the two legacy B2D data snapshots to Supabase.

The default mode is a local, read-only dry run. Passing ``--apply`` is the only
way to send data to Supabase. The script never prints project content, user
content, identifiers, paths, or credential values; its normal output is limited
to row counts and SHA-256 checksums.

Required environment variables:

* MIGRATION_OWNER_ID
* SUPABASE_URL
* SUPABASE_SERVICE_ROLE_KEY

The SQL migration in ``supabase/migrations`` must be applied first.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import mimetypes
import os
import re
import sqlite3
import sys
import time
import uuid
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence
from urllib.parse import quote

try:
    import httpx
except ImportError:  # pragma: no cover - exercised only in an incomplete runtime
    httpx = None  # type: ignore[assignment]


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CURRENT_DATA_DIR = PROJECT_ROOT / "data"
DEFAULT_LEGACY_DATA_DIR = PROJECT_ROOT.parent / "New folder2" / "data"

MIGRATION_NAMESPACE = uuid.UUID("5832a3ef-d5b4-42d6-87a6-1bd3be880405")
ZERO_UUID = uuid.UUID(int=0)

VALID_PROJECT_STATUSES = {
    "discovery",
    "ready_for_confirmation",
    "confirmed",
    "generating",
    "approved",
    "revised",
    "needs_attention",
}
VALID_AGENTS = {
    "discovery",
    "requirements",
    "architecture",
    "database",
    "api",
    "devops",
    "reviewer",
}
PROJECT_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
OUTPUT_FIELDS = (
    "business_analysis",
    "requirements",
    "architecture",
    "database",
    "api",
    "devops",
    "review",
)
OUTPUT_AGENT = {
    "business_analysis": "discovery",
    "requirements": "requirements",
    "architecture": "architecture",
    "database": "database",
    "api": "api",
    "devops": "devops",
    "review": "reviewer",
}

SENSITIVE_EXACT_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "credentials",
    "database_url",
    "jwt",
    "jwt_secret",
    "key",
    "password",
    "passwd",
    "postgres_url",
    "refresh_token",
    "secret",
    "service_role_key",
    "set_cookie",
    "token",
    "access_token",
    "client_secret",
    "connection_string",
}
SENSITIVE_KEY_SUFFIXES = (
    "_api_key",
    "_access_key",
    "_private_key",
    "_secret",
    "_token",
    "_password",
    "_passwd",
    "_credential",
    "_credentials",
    "_database_url",
    "_connection_string",
)

MIME_OVERRIDES = {
    ".md": "text/markdown",
    ".mmd": "text/vnd.mermaid",
    ".sql": "application/sql",
    ".yaml": "application/yaml",
    ".yml": "application/yaml",
}


class MigrationError(RuntimeError):
    """An error whose message is intentionally safe to print."""


@dataclass(frozen=True)
class SourceSpec:
    label: str
    data_dir: Path
    priority: int


@dataclass
class ProjectCandidate:
    project_id: str
    raw_context: dict[str, Any]
    updated_at: datetime
    source_priority: int


@dataclass
class FileArtifact:
    project_id: str
    name: str
    content: str
    byte_size: int
    sha256: str
    mime_type: str
    modified_at: datetime
    source_priority: int


@dataclass
class Inventory:
    projects: dict[str, ProjectCandidate] = field(default_factory=dict)
    run_records: list[dict[str, Any]] = field(default_factory=list)
    file_artifacts: dict[tuple[str, str], FileArtifact] = field(default_factory=dict)
    counts: dict[str, int] = field(default_factory=lambda: defaultdict(int))
    checksums: dict[str, str] = field(default_factory=dict)


@dataclass
class TargetRows:
    projects: list[dict[str, Any]]
    workflow_runs: list[dict[str, Any]]
    agent_runs: list[dict[str, Any]]
    api_calls: list[dict[str, Any]]
    artifacts: list[dict[str, Any]]
    transcript_specs: dict[str, list[dict[str, Any]]]


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Migrate legacy B2D SQLite/JSONL/artifact data to Supabase."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--apply",
        action="store_true",
        help="Perform idempotent Supabase upserts. Without this flag, no writes occur.",
    )
    mode.add_argument(
        "--dry-run",
        action="store_true",
        help="Explicitly select the default read-only validation mode.",
    )
    parser.add_argument(
        "--current-data-dir",
        type=Path,
        default=DEFAULT_CURRENT_DATA_DIR,
        help="Current backend data directory (default: B2D V1.1/data).",
    )
    parser.add_argument(
        "--legacy-data-dir",
        type=Path,
        default=DEFAULT_LEGACY_DATA_DIR,
        help="Legacy snapshot data directory (default: ../New folder2/data).",
    )
    parser.add_argument(
        "--batch-size",
        type=int,
        default=100,
        help="Rows per PostgREST upsert request (default: 100).",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=float,
        default=30.0,
        help="Per-request HTTP timeout when --apply is used (default: 30).",
    )
    parser.add_argument(
        "--max-artifact-bytes",
        type=int,
        default=2 * 1024 * 1024,
        help="Reject a single legacy text artifact above this size (default: 2 MiB).",
    )
    parser.add_argument(
        "--allow-owner-reassignment",
        action="store_true",
        help="Allow --apply to move an existing matching project ID to MIGRATION_OWNER_ID.",
    )
    args = parser.parse_args(argv)
    if args.batch_size < 1 or args.batch_size > 1000:
        parser.error("--batch-size must be between 1 and 1000")
    if args.timeout_seconds <= 0:
        parser.error("--timeout-seconds must be positive")
    if args.max_artifact_bytes < 1:
        parser.error("--max-artifact-bytes must be positive")
    return args


def load_required_environment() -> tuple[str, str, str]:
    names = (
        "MIGRATION_OWNER_ID",
        "SUPABASE_URL",
        "SUPABASE_SERVICE_ROLE_KEY",
    )
    values = {name: os.environ.get(name, "").strip() for name in names}
    missing = [name for name in names if not values[name]]
    if missing:
        raise MigrationError(
            "missing required environment variable names: " + ",".join(missing)
        )
    try:
        owner_id = str(uuid.UUID(values["MIGRATION_OWNER_ID"]))
    except ValueError as exc:
        raise MigrationError("MIGRATION_OWNER_ID is not a valid UUID") from exc
    supabase_url = values["SUPABASE_URL"].rstrip("/")
    if not supabase_url.startswith("https://"):
        raise MigrationError("SUPABASE_URL must use https")
    return owner_id, supabase_url, values["SUPABASE_SERVICE_ROLE_KEY"]


def is_sensitive_key(key: object) -> bool:
    normalized = re.sub(r"[^a-z0-9]+", "_", str(key).strip().lower()).strip("_")
    return normalized in SENSITIVE_EXACT_KEYS or normalized.endswith(SENSITIVE_KEY_SUFFIXES)


def normalize_json(value: Any) -> Any:
    """Return a JSON-safe value without changing legitimate canonical text."""
    if isinstance(value, Mapping):
        return {str(key): normalize_json(child) for key, child in value.items()}
    if isinstance(value, (list, tuple)):
        return [normalize_json(child) for child in value]
    if isinstance(value, str):
        return value
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return str(value)


def redact_metadata(value: Any) -> Any:
    """Redact secret-keyed values only inside telemetry/API/run metadata.

    Canonical project context, message text, and artifact content deliberately
    do not pass through this function. This boundary preserves business-domain
    fields legitimately named ``authorization``, ``token``, or ``password``.
    """
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, child in value.items():
            key = str(raw_key)
            result[key] = (
                "[REDACTED]" if is_sensitive_key(key) else redact_metadata(child)
            )
        return result
    if isinstance(value, (list, tuple)):
        return [redact_metadata(child) for child in value]
    return normalize_json(value)


def as_object(value: Any, *, sanitize_metadata: bool = False) -> dict[str, Any]:
    clean = redact_metadata(value) if sanitize_metadata else normalize_json(value)
    if isinstance(clean, dict):
        return clean
    if clean is None:
        return {}
    return {"value": clean}


def run_fidelity_self_test() -> int:
    """Prove canonical and metadata pipelines have intentionally different rules."""
    legitimate = {
        "authorization": "JWT bearer authorization requirement",
        "token": "Domain token entity",
        "password": "Password reset requirement",
    }
    canonical_project = as_object(legitimate)
    canonical_artifact_structured = as_object(legitimate)
    artifact_content_input = json.dumps(legitimate, ensure_ascii=False)
    message_input = json.dumps(legitimate, ensure_ascii=False)
    canonical_artifact_content = normalize_json(artifact_content_input)
    canonical_message = normalize_json(message_input)

    checks = 0
    for field_name, expected in legitimate.items():
        if canonical_project.get(field_name) != expected:
            raise MigrationError("canonical project fidelity self-test failed")
        checks += 1
        if canonical_artifact_structured.get(field_name) != expected:
            raise MigrationError("canonical artifact fidelity self-test failed")
        checks += 1
    if canonical_artifact_content != artifact_content_input:
        raise MigrationError("canonical artifact text fidelity self-test failed")
    checks += 1
    if canonical_message != message_input:
        raise MigrationError("canonical message fidelity self-test failed")
    checks += 1

    sanitized_metadata = as_object(legitimate, sanitize_metadata=True)
    for value in sanitized_metadata.values():
        if value != "[REDACTED]":
            raise MigrationError("metadata redaction self-test failed")
        checks += 1
    return checks


def canonical_bytes(value: Any) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    ).encode("utf-8")


def checksum(value: Any) -> str:
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def file_checksum(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def parse_datetime(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    candidate = value.strip()
    if candidate.endswith("Z"):
        candidate = candidate[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def isoformat_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat()


def deterministic_uuid(kind: str, *parts: object) -> str:
    material = ":".join([kind, *(str(part) for part in parts)])
    return str(uuid.uuid5(MIGRATION_NAMESPACE, material))


def readonly_sqlite_connection(path: Path) -> sqlite3.Connection:
    encoded = quote(path.resolve().as_posix(), safe="/:")
    connection = sqlite3.connect(f"file:{encoded}?mode=ro", uri=True)
    connection.row_factory = sqlite3.Row
    return connection


def load_sqlite_projects(source: SourceSpec, inventory: Inventory) -> None:
    database = source.data_dir / "b2d.db"
    if not database.is_file():
        raise MigrationError(f"{source.label} SQLite database is missing")
    inventory.checksums[f"source.sqlite.{source.label}"] = file_checksum(database)
    try:
        with readonly_sqlite_connection(database) as connection:
            table = connection.execute(
                "select 1 from sqlite_master where type='table' and name='projects'"
            ).fetchone()
            if table is None:
                raise MigrationError(f"{source.label} SQLite projects table is missing")
            rows = connection.execute(
                "select project_id, data, status, updated_at from projects order by project_id"
            ).fetchall()
    except sqlite3.Error as exc:
        raise MigrationError(f"{source.label} SQLite read failed") from exc

    inventory.counts[f"source.sqlite.{source.label}.rows"] += len(rows)
    for row_number, row in enumerate(rows, start=1):
        project_id = str(row["project_id"] or "").strip()
        if not project_id:
            raise MigrationError(
                f"{source.label} SQLite contains a blank project ID at row {row_number}"
            )
        if not PROJECT_ID_PATTERN.fullmatch(project_id):
            raise MigrationError(
                f"{source.label} SQLite contains an unsafe project ID at row {row_number}"
            )
        try:
            raw = json.loads(row["data"])
        except (TypeError, json.JSONDecodeError) as exc:
            raise MigrationError(
                f"{source.label} SQLite contains invalid project JSON at row {row_number}"
            ) from exc
        if not isinstance(raw, dict):
            raise MigrationError(
                f"{source.label} SQLite project JSON is not an object at row {row_number}"
            )
        raw["project_id"] = project_id
        if not raw.get("status") and row["status"]:
            raw["status"] = row["status"]
        updated = (
            parse_datetime(row["updated_at"])
            or parse_datetime(raw.get("updated_at"))
            or datetime(1970, 1, 1, tzinfo=timezone.utc)
        )
        candidate = ProjectCandidate(project_id, raw, updated, source.priority)
        existing = inventory.projects.get(project_id)
        if existing is None:
            inventory.projects[project_id] = candidate
            continue
        inventory.counts["source.sqlite.merged_duplicates"] += 1
        if (candidate.updated_at, candidate.source_priority) >= (
            existing.updated_at,
            existing.source_priority,
        ):
            inventory.projects[project_id] = candidate


def load_run_records(source: SourceSpec, inventory: Inventory) -> None:
    runs_dir = source.data_dir / "runs"
    if not runs_dir.is_dir():
        inventory.checksums[f"source.runs.{source.label}"] = checksum([])
        return

    records: list[dict[str, Any]] = []
    for jsonl in sorted(runs_dir.glob("*.jsonl")):
        if not jsonl.is_file():
            continue
        try:
            lines = jsonl.read_text(encoding="utf-8").splitlines()
        except (OSError, UnicodeError) as exc:
            raise MigrationError(f"{source.label} JSONL read failed") from exc
        for line_number, line in enumerate(lines, start=1):
            if not line.strip():
                continue
            try:
                value = json.loads(line)
            except json.JSONDecodeError as exc:
                raise MigrationError(
                    f"{source.label} JSONL contains invalid JSON at line {line_number}"
                ) from exc
            if not isinstance(value, dict):
                raise MigrationError(
                    f"{source.label} JSONL record is not an object at line {line_number}"
                )
            value["_source_priority"] = source.priority
            records.append(value)

    inventory.counts[f"source.runs.{source.label}.records"] += len(records)
    inventory.checksums[f"source.runs.{source.label}"] = checksum(normalize_json(records))
    inventory.run_records.extend(records)


def infer_mime_type(name: str) -> str:
    if Path(name).name.lower() == "dockerfile":
        return "text/x-dockerfile"
    suffix = Path(name).suffix.lower()
    if suffix in MIME_OVERRIDES:
        return MIME_OVERRIDES[suffix]
    guessed, _encoding = mimetypes.guess_type(name)
    return guessed or "text/plain"


def load_file_artifacts(
    source: SourceSpec,
    inventory: Inventory,
    *,
    max_artifact_bytes: int,
) -> None:
    artifacts_dir = source.data_dir / "artifacts"
    source_digest_rows: list[dict[str, Any]] = []
    if not artifacts_dir.is_dir():
        inventory.checksums[f"source.artifacts.{source.label}"] = checksum([])
        return

    for project_dir in sorted(artifacts_dir.iterdir()):
        if not project_dir.is_dir() or project_dir.is_symlink():
            continue
        project_id = project_dir.name
        for path in sorted(project_dir.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            try:
                resolved = path.resolve(strict=True)
                resolved.relative_to(project_dir.resolve(strict=True))
            except (OSError, ValueError) as exc:
                raise MigrationError(f"{source.label} artifact escaped its project directory") from exc
            size = resolved.stat().st_size
            if size > max_artifact_bytes:
                raise MigrationError(
                    f"{source.label} artifact exceeds --max-artifact-bytes"
                )
            try:
                raw_bytes = resolved.read_bytes()
                raw_text = raw_bytes.decode("utf-8-sig")
            except (OSError, UnicodeError) as exc:
                raise MigrationError(f"{source.label} artifact is not readable UTF-8 text") from exc
            content = raw_text
            content_bytes = content.encode("utf-8")
            name = resolved.relative_to(project_dir.resolve(strict=True)).as_posix()
            modified_at = datetime.fromtimestamp(resolved.stat().st_mtime, tz=timezone.utc)
            artifact = FileArtifact(
                project_id=project_id,
                name=name,
                content=content,
                byte_size=len(content_bytes),
                sha256=hashlib.sha256(content_bytes).hexdigest(),
                mime_type=infer_mime_type(name),
                modified_at=modified_at,
                source_priority=source.priority,
            )
            key = (project_id, name)
            existing = inventory.file_artifacts.get(key)
            if existing is None or (artifact.modified_at, artifact.source_priority) >= (
                existing.modified_at,
                existing.source_priority,
            ):
                if existing is not None:
                    inventory.counts["source.artifacts.merged_duplicates"] += 1
                inventory.file_artifacts[key] = artifact
            source_digest_rows.append(
                {
                    "project_id": project_id,
                    "name": name,
                    "byte_size": len(raw_bytes),
                    "sha256": hashlib.sha256(raw_bytes).hexdigest(),
                }
            )

    inventory.counts[f"source.artifacts.{source.label}.files"] += len(source_digest_rows)
    inventory.checksums[f"source.artifacts.{source.label}"] = checksum(source_digest_rows)


def load_inventory(sources: Sequence[SourceSpec], max_artifact_bytes: int) -> Inventory:
    inventory = Inventory()
    for source in sources:
        load_sqlite_projects(source, inventory)
        load_run_records(source, inventory)
        load_file_artifacts(
            source,
            inventory,
            max_artifact_bytes=max_artifact_bytes,
        )

    # Exact duplicate JSONL records may exist when a data directory was copied.
    deduplicated: dict[str, dict[str, Any]] = {}
    for record in inventory.run_records:
        digest = checksum(
            normalize_json(
                {k: v for k, v in record.items() if k != "_source_priority"}
            )
        )
        existing = deduplicated.get(digest)
        if existing is None or int(record.get("_source_priority", 0)) >= int(
            existing.get("_source_priority", 0)
        ):
            if existing is not None:
                inventory.counts["source.runs.merged_duplicates"] += 1
            deduplicated[digest] = record
    inventory.run_records = list(deduplicated.values())
    inventory.counts["source.projects.unique"] = len(inventory.projects)
    inventory.counts["source.runs.unique_records"] = len(inventory.run_records)
    inventory.counts["source.artifacts.unique_files"] = len(inventory.file_artifacts)
    return inventory


def derive_title(business_idea: str, project_id: str) -> str:
    compact = " ".join(business_idea.split())
    return (compact[:120] if compact else project_id[:120]) or "Untitled project"


def project_created_at(raw: Mapping[str, Any], fallback: datetime) -> datetime:
    timestamps: list[datetime] = []
    transcript = raw.get("transcript")
    if isinstance(transcript, list):
        for turn in transcript:
            if isinstance(turn, Mapping):
                parsed = parse_datetime(turn.get("timestamp"))
                if parsed is not None:
                    timestamps.append(parsed)
    return min(timestamps) if timestamps else fallback


def normalize_project_status(value: Any) -> str:
    status = str(value or "discovery").strip().lower()
    return status if status in VALID_PROJECT_STATUSES else "needs_attention"


def workflow_status(project_status: str) -> str:
    if project_status in {"approved", "revised", "needs_attention"}:
        return project_status
    return "needs_attention"


def pair_run_records(
    records: Sequence[dict[str, Any]],
    known_projects: set[str],
) -> tuple[list[dict[str, Any]], int]:
    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    orphaned = 0
    for raw in records:
        project_id = str(raw.get("project_id") or "").strip()
        agent = str(raw.get("agent") or "").strip().lower()
        if project_id not in known_projects or agent not in VALID_AGENTS:
            orphaned += 1
            continue
        started = parse_datetime(raw.get("started_at"))
        if started is None:
            orphaned += 1
            continue
        key = (project_id, agent, isoformat_utc(started))
        grouped[key].append(raw)

    selected: list[dict[str, Any]] = []
    for (project_id, agent, started_iso), group in grouped.items():
        def record_rank(item: Mapping[str, Any]) -> tuple[int, datetime, int]:
            status = str(item.get("status") or "started").lower()
            terminal = 1 if status in {"success", "failed", "cancelled"} else 0
            completed = parse_datetime(item.get("completed_at")) or datetime(
                1970, 1, 1, tzinfo=timezone.utc
            )
            priority = int(item.get("_source_priority", 0))
            return terminal, completed, priority

        winner = max(group, key=record_rank)
        clean = dict(winner)
        clean["project_id"] = project_id
        clean["agent"] = agent
        clean["started_at"] = started_iso
        selected.append(clean)

    selected.sort(key=lambda item: (item["project_id"], item["agent"], item["started_at"]))
    invocation_by_agent: dict[tuple[str, str], int] = defaultdict(int)
    for item in selected:
        key = (item["project_id"], item["agent"])
        invocation_by_agent[key] += 1
        item["_invocation"] = invocation_by_agent[key]
    return selected, orphaned


def build_target_rows(inventory: Inventory, owner_id: str) -> TargetRows:
    paired_runs, orphaned_runs = pair_run_records(
        inventory.run_records, set(inventory.projects)
    )
    inventory.counts["source.runs.orphaned_or_invalid"] = orphaned_runs

    runs_by_project: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for run in paired_runs:
        runs_by_project[run["project_id"]].append(run)
    files_by_project: dict[str, list[FileArtifact]] = defaultdict(list)
    for artifact in inventory.file_artifacts.values():
        if artifact.project_id in inventory.projects:
            files_by_project[artifact.project_id].append(artifact)
        else:
            inventory.counts["source.artifacts.orphaned"] += 1

    project_rows: list[dict[str, Any]] = []
    workflow_rows: list[dict[str, Any]] = []
    transcript_specs: dict[str, list[dict[str, Any]]] = {}
    output_specs: list[tuple[str, str, dict[str, Any], str]] = []
    workflow_id_by_project: dict[str, str] = {}

    for project_id in sorted(inventory.projects):
        candidate = inventory.projects[project_id]
        raw = as_object(candidate.raw_context)
        business_idea = str(raw.get("business_idea") or "").strip()
        if not business_idea:
            business_idea = "Imported legacy project"
        status = normalize_project_status(raw.get("status"))
        updated = candidate.updated_at
        created = project_created_at(raw, updated)

        available_outputs = [name for name in OUTPUT_FIELDS if raw.get(name) is not None]
        previous_generation_state = as_object(raw.get("generation_state"))
        generation_state = {
            **previous_generation_state,
            "imported_from_legacy": True,
            "legacy_status": status,
            "latest_outputs": available_outputs,
            "legacy_updated_at": isoformat_utc(updated),
        }
        # SupabaseProjectStore.load() validates this JSONB value directly as a
        # ProjectContext. Keep the complete latest context here; normalized
        # messages/artifacts are additional query-friendly projections.
        context = dict(raw)
        context.update(
            {
                "project_id": project_id,
                "business_idea": business_idea,
                "status": status,
                "generation_state": generation_state,
                "updated_at": isoformat_utc(updated),
            }
        )
        project_rows.append(
            {
                "id": project_id,
                "user_id": owner_id,
                "business_idea": business_idea,
                "title": derive_title(business_idea, project_id),
                "status": status,
                "context": context,
                "generation_state": generation_state,
                "created_at": isoformat_utc(created),
                "updated_at": isoformat_utc(updated),
            }
        )

        transcript = raw.get("transcript")
        transcript_specs[project_id] = transcript if isinstance(transcript, list) else []

        for output_name in available_outputs:
            output_specs.append(
                (project_id, output_name, as_object(raw[output_name]), isoformat_utc(updated))
            )

        has_activity = bool(available_outputs or runs_by_project[project_id] or files_by_project[project_id])
        if has_activity:
            workflow_id = deterministic_uuid("workflow", project_id)
            workflow_id_by_project[project_id] = workflow_id
            run_times = [
                parse_datetime(item.get("started_at"))
                for item in runs_by_project[project_id]
            ]
            run_times = [value for value in run_times if value is not None]
            completed_times = [
                parse_datetime(item.get("completed_at"))
                for item in runs_by_project[project_id]
            ]
            completed_times = [value for value in completed_times if value is not None]
            started_at = min(run_times) if run_times else created
            completed_at = max(completed_times) if completed_times else updated
            workflow_rows.append(
                {
                    "id": workflow_id,
                    "project_id": project_id,
                    "conversation_id": None,
                    "user_id": owner_id,
                    "status": workflow_status(status),
                    "current_stage": status,
                    "idempotency_key": f"legacy-import-v1:{project_id}",
                    "context_snapshot": context,
                    "summary": {
                        "imported_from_legacy": True,
                        "agent_outputs": available_outputs,
                        "paired_agent_runs": len(runs_by_project[project_id]),
                        "rendered_artifacts": len(files_by_project[project_id]),
                    },
                    "error": {},
                    "created_at": isoformat_utc(started_at),
                    "started_at": isoformat_utc(started_at),
                    "completed_at": isoformat_utc(max(completed_at, started_at)),
                    "updated_at": isoformat_utc(updated),
                }
            )

    agent_rows: list[dict[str, Any]] = []
    api_rows: list[dict[str, Any]] = []
    latest_agent_run: dict[tuple[str, str], tuple[int, str]] = {}
    for raw in paired_runs:
        project_id = raw["project_id"]
        workflow_id = workflow_id_by_project.get(project_id)
        if workflow_id is None:
            continue
        agent = raw["agent"]
        invocation = int(raw.get("_invocation", 1))
        started = parse_datetime(raw.get("started_at")) or datetime.now(timezone.utc)
        completed = parse_datetime(raw.get("completed_at"))
        if completed is not None and completed < started:
            completed = started
        status = str(raw.get("status") or "started").lower()
        if status not in {"started", "retrying", "success", "failed", "cancelled"}:
            status = "failed"
        duration_ms = raw.get("duration_ms")
        if not isinstance(duration_ms, int) or duration_ms < 0:
            duration_ms = None
        model = str(raw.get("model") or "") or None
        provider = str(raw.get("provider") or "") or None
        if provider is None and model:
            provider = "gemini" if "gemini" in model.lower() else "legacy"
        external_call_id = str(raw.get("call_id") or "") or None
        error_text = raw.get("error")
        if isinstance(error_text, Mapping):
            error = json.dumps(
                redact_metadata(error_text),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )
        else:
            error = str(error_text) if error_text else None
        telemetry = redact_metadata(
            {
                key: raw[key]
                for key in (
                    "retry_count",
                    "input_chars",
                    "output_chars",
                    "schema_chars",
                    "ttft_s",
                    "input_tokens",
                    "output_tokens",
                )
                if key in raw and raw[key] is not None
            }
        )
        agent_run_id = deterministic_uuid(
            "agent-run", project_id, agent, raw["started_at"]
        )
        agent_rows.append(
            {
                "id": agent_run_id,
                "workflow_run_id": workflow_id,
                "project_id": project_id,
                "agent": agent,
                "invocation": invocation,
                "attempt": 0,
                "status": status,
                "provider": provider,
                "model": model,
                "external_call_id": external_call_id,
                "input_data": as_object(raw.get("input"), sanitize_metadata=True),
                "output_data": as_object(raw.get("output"), sanitize_metadata=True),
                "error": error,
                "telemetry": telemetry,
                "retry_count": int(raw.get("retry_count") or 0),
                "input_chars": int(raw.get("input_chars") or 0),
                "output_chars": int(raw.get("output_chars") or 0),
                "started_at": isoformat_utc(started),
                "completed_at": isoformat_utc(completed) if completed else None,
                "duration_ms": duration_ms,
                "created_at": isoformat_utc(started),
                "updated_at": isoformat_utc(completed or started),
            }
        )
        current = latest_agent_run.get((project_id, agent))
        if current is None or invocation >= current[0]:
            latest_agent_run[(project_id, agent)] = (invocation, agent_run_id)

        api_status = status if status in {"started", "success", "failed"} else "failed"
        api_rows.append(
            {
                "id": deterministic_uuid("api-call", agent_run_id),
                "project_id": project_id,
                "workflow_run_id": workflow_id,
                "agent_run_id": agent_run_id,
                "provider": provider or "legacy",
                "operation": "generateContent" if provider == "gemini" else "agent_generation",
                "status": api_status,
                "external_call_id": external_call_id,
                "http_status": None,
                "request_metadata": {
                    "agent": agent,
                    "input_chars": telemetry.get("input_chars", 0),
                },
                "response_metadata": {
                    "output_chars": telemetry.get("output_chars", 0),
                },
                "telemetry": telemetry,
                "error": {"message": error} if error else {},
                "sanitized": True,
                "started_at": isoformat_utc(started),
                "completed_at": isoformat_utc(completed) if completed else None,
                "duration_ms": duration_ms,
                "created_at": isoformat_utc(started),
                "updated_at": isoformat_utc(completed or started),
            }
        )

    artifact_rows: list[dict[str, Any]] = []
    for project_id, output_name, output, updated_at in sorted(output_specs):
        serialized = canonical_bytes(output)
        agent_name = OUTPUT_AGENT[output_name]
        latest = latest_agent_run.get((project_id, agent_name))
        artifact_rows.append(
            {
                "id": deterministic_uuid("artifact", project_id, f"{output_name}.json"),
                "project_id": project_id,
                "workflow_run_id": workflow_id_by_project.get(project_id),
                "agent_run_id": latest[1] if latest else None,
                "name": f"{output_name}.json",
                "artifact_type": output_name,
                "content_text": None,
                "structured_data": output,
                "mime_type": "application/json",
                "byte_size": len(serialized),
                "sha256": hashlib.sha256(serialized).hexdigest(),
                "metadata": {"imported_from": "project_context"},
                "created_at": updated_at,
                "updated_at": updated_at,
            }
        )

    for key in sorted(inventory.file_artifacts):
        file_artifact = inventory.file_artifacts[key]
        if file_artifact.project_id not in inventory.projects:
            continue
        updated_at = isoformat_utc(file_artifact.modified_at)
        artifact_type = Path(file_artifact.name).suffix.lower().lstrip(".") or "file"
        artifact_rows.append(
            {
                "id": deterministic_uuid(
                    "artifact", file_artifact.project_id, file_artifact.name
                ),
                "project_id": file_artifact.project_id,
                "workflow_run_id": workflow_id_by_project.get(file_artifact.project_id),
                "agent_run_id": None,
                "name": file_artifact.name,
                "artifact_type": artifact_type,
                "content_text": file_artifact.content,
                "structured_data": {},
                "mime_type": file_artifact.mime_type,
                "byte_size": file_artifact.byte_size,
                "sha256": file_artifact.sha256,
                "metadata": {"imported_from": "legacy_artifact_file"},
                "created_at": updated_at,
                "updated_at": updated_at,
            }
        )

    return TargetRows(
        projects=project_rows,
        workflow_runs=workflow_rows,
        agent_runs=agent_rows,
        api_calls=api_rows,
        artifacts=artifact_rows,
        transcript_specs=transcript_specs,
    )


def build_message_rows(
    target: TargetRows,
    conversation_ids: Mapping[str, str],
    owner_id: str,
) -> list[dict[str, Any]]:
    projects_by_id = {row["id"]: row for row in target.projects}
    rows: list[dict[str, Any]] = []
    for project_id in sorted(target.transcript_specs):
        conversation_id = conversation_ids[project_id]
        project_created = parse_datetime(projects_by_id[project_id]["created_at"]) or datetime(
            1970, 1, 1, tzinfo=timezone.utc
        )
        for turn_index, turn in enumerate(target.transcript_specs[project_id]):
            if not isinstance(turn, Mapping):
                continue
            legacy_role = str(turn.get("role") or "agent").strip().lower()
            role = "assistant" if legacy_role == "agent" else legacy_role
            if role not in {"user", "assistant", "system", "agent", "tool"}:
                role = "assistant"
            content = str(turn.get("message") or "")
            if role == "user" and not content.strip():
                continue
            created = parse_datetime(turn.get("timestamp")) or (
                project_created + timedelta(microseconds=turn_index)
            )
            rows.append(
                {
                    "id": deterministic_uuid(
                        "message",
                        project_id,
                        turn_index,
                        legacy_role,
                        checksum(content)[:16],
                    ),
                    "conversation_id": conversation_id,
                    "project_id": project_id,
                    "sender_user_id": owner_id if role == "user" else None,
                    "role": role,
                    "turn_index": turn_index,
                    "content": content,
                    "structured_data": {},
                    "metadata": {
                        "imported_from": "project_context_transcript",
                        "legacy_role": legacy_role,
                    },
                    "client_message_id": None,
                    "created_at": isoformat_utc(created),
                }
            )
    return rows


def metadata_is_sanitized(value: Any) -> bool:
    """Return whether every secret-keyed metadata value is redacted."""
    if isinstance(value, Mapping):
        for key, child in value.items():
            if is_sensitive_key(key):
                if child != "[REDACTED]":
                    return False
            elif not metadata_is_sanitized(child):
                return False
    elif isinstance(value, (list, tuple)):
        return all(metadata_is_sanitized(child) for child in value)
    return True


def validate_target_rows(
    target: TargetRows,
    messages: Sequence[dict[str, Any]],
    owner_id: str,
) -> int:
    """Validate the complete payload locally before the first network write.

    Error messages intentionally identify only a table/invariant, never a row,
    project ID, content value, or filesystem path.
    """
    checks = 0

    def require(condition: bool, safe_message: str) -> None:
        nonlocal checks
        checks += 1
        if not condition:
            raise MigrationError(safe_message)

    tables: dict[str, Sequence[dict[str, Any]]] = {
        "projects": target.projects,
        "workflow_runs": target.workflow_runs,
        "agent_runs": target.agent_runs,
        "api_calls": target.api_calls,
        "artifacts": target.artifacts,
        "messages": messages,
    }
    for label, rows in tables.items():
        row_ids = [str(row.get("id") or "") for row in rows]
        require(
            all(row_ids) and len(row_ids) == len(set(row_ids)),
            f"target {label} IDs are blank or duplicated",
        )

    project_ids = {str(row["id"]) for row in target.projects}
    workflow_ids = {str(row["id"]) for row in target.workflow_runs}
    agent_run_ids = {str(row["id"]) for row in target.agent_runs}

    for row in target.projects:
        project_id = str(row["id"])
        context = row.get("context")
        generation_state = row.get("generation_state")
        require(
            PROJECT_ID_PATTERN.fullmatch(project_id) is not None,
            "target project ID is invalid",
        )
        require(row.get("user_id") == owner_id, "target project owner is invalid")
        require(
            bool(str(row.get("business_idea") or "").strip()),
            "target project business idea is blank",
        )
        require(
            bool(str(row.get("title") or "").strip()),
            "target project title is blank",
        )
        require(
            row.get("status") in VALID_PROJECT_STATUSES,
            "target project status is invalid",
        )
        require(isinstance(context, Mapping), "target project context is not an object")
        require(
            isinstance(context, Mapping) and context.get("project_id") == project_id,
            "target project context identity is invalid",
        )
        require(
            isinstance(generation_state, Mapping),
            "target project generation state is not an object",
        )
        require(
            isinstance(context, Mapping)
            and context.get("generation_state") == generation_state,
            "target project generation state is inconsistent",
        )

    for row in target.workflow_runs:
        require(
            row.get("project_id") in project_ids,
            "target workflow project reference is invalid",
        )
        require(row.get("user_id") == owner_id, "target workflow owner is invalid")
        require(
            isinstance(row.get("context_snapshot"), Mapping),
            "target workflow context snapshot is not an object",
        )
        require(
            isinstance(row.get("summary"), Mapping)
            and metadata_is_sanitized(row.get("summary")),
            "target workflow summary metadata is not sanitized",
        )
        require(
            isinstance(row.get("error"), Mapping)
            and metadata_is_sanitized(row.get("error")),
            "target workflow error metadata is not sanitized",
        )

    for row in target.agent_runs:
        require(
            row.get("project_id") in project_ids,
            "target agent-run project reference is invalid",
        )
        require(
            row.get("workflow_run_id") in workflow_ids,
            "target agent-run workflow reference is invalid",
        )
        require(row.get("agent") in VALID_AGENTS, "target agent-run agent is invalid")
        for field_name in ("input_data", "output_data", "telemetry"):
            field_value = row.get(field_name)
            require(
                isinstance(field_value, Mapping)
                and metadata_is_sanitized(field_value),
                f"target agent-run {field_name} is not sanitized metadata",
            )

    for row in target.api_calls:
        require(
            row.get("project_id") in project_ids,
            "target API-call project reference is invalid",
        )
        require(
            row.get("workflow_run_id") in workflow_ids,
            "target API-call workflow reference is invalid",
        )
        require(
            row.get("agent_run_id") in agent_run_ids,
            "target API-call agent-run reference is invalid",
        )
        require(row.get("sanitized") is True, "target API-call sanitized flag is false")
        for field_name in (
            "request_metadata",
            "response_metadata",
            "telemetry",
            "error",
        ):
            field_value = row.get(field_name)
            require(
                isinstance(field_value, Mapping)
                and metadata_is_sanitized(field_value),
                f"target API-call {field_name} is not sanitized metadata",
            )

    artifact_keys: set[tuple[str, str]] = set()
    for row in target.artifacts:
        project_id = str(row.get("project_id") or "")
        name = str(row.get("name") or "")
        artifact_key = (project_id, name)
        require(project_id in project_ids, "target artifact project reference is invalid")
        require(bool(name.strip()), "target artifact name is blank")
        require(
            artifact_key not in artifact_keys,
            "target artifact project/name pair is duplicated",
        )
        artifact_keys.add(artifact_key)
        structured_data = row.get("structured_data")
        content_text = row.get("content_text")
        require(
            isinstance(structured_data, Mapping),
            "target artifact structured data is not an object",
        )
        require(
            content_text is None or isinstance(content_text, str),
            "target artifact content is not text",
        )
        payload = (
            content_text.encode("utf-8")
            if isinstance(content_text, str)
            else canonical_bytes(structured_data)
        )
        require(row.get("byte_size") == len(payload), "target artifact byte size is invalid")
        require(
            row.get("sha256") == hashlib.sha256(payload).hexdigest(),
            "target artifact checksum is invalid",
        )
        require(
            isinstance(row.get("metadata"), Mapping)
            and metadata_is_sanitized(row.get("metadata")),
            "target artifact metadata is not sanitized",
        )

    message_turns: set[tuple[str, int]] = set()
    for row in messages:
        role = row.get("role")
        turn_key = (str(row.get("conversation_id") or ""), int(row.get("turn_index", -1)))
        require(
            row.get("project_id") in project_ids,
            "target message project reference is invalid",
        )
        require(role in {"user", "assistant", "system", "agent", "tool"}, "target message role is invalid")
        require(
            (role == "user" and row.get("sender_user_id") == owner_id)
            or (role != "user" and row.get("sender_user_id") is None),
            "target message sender is invalid",
        )
        require(isinstance(row.get("content"), str), "target message content is not text")
        require(turn_key[1] >= 0, "target message turn index is invalid")
        require(turn_key not in message_turns, "target message turn is duplicated")
        message_turns.add(turn_key)
        require(
            isinstance(row.get("structured_data"), Mapping),
            "target message structured data is not an object",
        )
        require(
            isinstance(row.get("metadata"), Mapping)
            and metadata_is_sanitized(row.get("metadata")),
            "target message metadata is not sanitized",
        )

    return checks


def placeholder_conversations(project_ids: Iterable[str]) -> dict[str, str]:
    return {
        project_id: deterministic_uuid("dry-run-conversation", project_id)
        for project_id in project_ids
    }


def chunks(rows: Sequence[dict[str, Any]], size: int) -> Iterable[list[dict[str, Any]]]:
    for index in range(0, len(rows), size):
        yield list(rows[index : index + size])


class SupabaseRest:
    def __init__(self, url: str, service_role_key: str, timeout_seconds: float):
        if httpx is None:
            raise MigrationError("httpx is required; install the project requirements first")
        self._base = url.rstrip("/") + "/rest/v1"
        self._client = httpx.Client(
            timeout=timeout_seconds,
            headers={
                "apikey": service_role_key,
                "Authorization": f"Bearer {service_role_key}",
                "Accept": "application/json",
                "Content-Type": "application/json",
                "Accept-Profile": "public",
                "Content-Profile": "public",
            },
        )

    def close(self) -> None:
        self._client.close()

    def _request(
        self,
        method: str,
        table: str,
        *,
        params: Mapping[str, str] | None = None,
        json_body: Any = None,
        prefer: str | None = None,
    ) -> Any:
        headers = {"Prefer": prefer} if prefer else None
        last_status: int | None = None
        for attempt in range(4):
            try:
                response = self._client.request(
                    method,
                    f"{self._base}/{table}",
                    params=params,
                    json=json_body,
                    headers=headers,
                )
            except httpx.HTTPError as exc:
                if attempt == 3:
                    raise MigrationError(
                        f"Supabase network request failed for table {table}"
                    ) from exc
                time.sleep(0.25 * (2**attempt))
                continue
            last_status = response.status_code
            if response.status_code in {408, 429} or response.status_code >= 500:
                if attempt < 3:
                    time.sleep(0.25 * (2**attempt))
                    continue
            if response.is_error:
                raise MigrationError(
                    f"Supabase request failed for table {table} with status {response.status_code}"
                )
            if not response.content:
                return None
            try:
                return response.json()
            except ValueError as exc:
                raise MigrationError(
                    f"Supabase returned non-JSON data for table {table}"
                ) from exc
        raise MigrationError(
            f"Supabase request failed for table {table} with status {last_status or 0}"
        )

    def upsert(
        self,
        table: str,
        rows: Sequence[dict[str, Any]],
        *,
        on_conflict: str,
        batch_size: int,
    ) -> None:
        for batch in chunks(rows, batch_size):
            self._request(
                "POST",
                table,
                params={"on_conflict": on_conflict},
                json_body=batch,
                prefer="resolution=merge-duplicates,return=minimal",
            )

    def get_rows(
        self,
        table: str,
        *,
        params: Mapping[str, str],
    ) -> list[dict[str, Any]]:
        result = self._request("GET", table, params=params)
        if not isinstance(result, list):
            raise MigrationError(f"Supabase returned an invalid row set for table {table}")
        return [row for row in result if isinstance(row, dict)]

    def check_owner_profile(self, owner_id: str) -> None:
        rows = self.get_rows(
            "profiles",
            params={"select": "id", "id": f"eq.{owner_id}", "limit": "1"},
        )
        if len(rows) != 1:
            raise MigrationError(
                "MIGRATION_OWNER_ID does not have a profile; create the Auth user first"
            )

    def existing_project_states(
        self,
        project_ids: Sequence[str],
    ) -> dict[str, dict[str, Any]]:
        states: dict[str, dict[str, Any]] = {}
        for project_batch in _string_chunks(project_ids, 50):
            rows = self.get_rows(
                "projects",
                params={
                    "select": (
                        "id,user_id,context_version,stage_lease_token,"
                        "stage_lease_name,stage_lease_expires_at"
                    ),
                    "id": "in.(" + ",".join(project_batch) + ")",
                },
            )
            for row in rows:
                if row.get("id") and row.get("user_id"):
                    states[str(row["id"])] = row
        return states

    def default_conversations(self, project_ids: Sequence[str]) -> dict[str, str]:
        mapping: dict[str, str] = {}
        for project_batch in _string_chunks(project_ids, 50):
            rows = self.get_rows(
                "conversations",
                params={
                    "select": "id,project_id",
                    "is_default": "eq.true",
                    "project_id": "in.(" + ",".join(project_batch) + ")",
                },
            )
            for row in rows:
                if row.get("id") and row.get("project_id"):
                    mapping[str(row["project_id"])] = str(row["id"])
        if len(mapping) != len(set(project_ids)):
            raise MigrationError("default conversation lookup returned an incomplete count")
        return mapping


def _string_chunks(values: Sequence[str], size: int) -> Iterable[list[str]]:
    for index in range(0, len(values), size):
        yield list(values[index : index + size])


def apply_rows(
    rest: SupabaseRest,
    target: TargetRows,
    owner_id: str,
    *,
    batch_size: int,
    allow_owner_reassignment: bool,
) -> list[dict[str, Any]]:
    project_ids = [row["id"] for row in target.projects]
    rest.check_owner_profile(owner_id)
    existing = rest.existing_project_states(project_ids)
    mismatched = sum(
        1 for state in existing.values() if str(state.get("user_id")) != owner_id
    )
    if mismatched and not allow_owner_reassignment:
        raise MigrationError(
            "existing project owner mismatch count is nonzero; use "
            "--allow-owner-reassignment only after reviewing ownership"
        )

    versioned_or_leased = 0
    invalid_concurrency_state = 0
    for state in existing.values():
        version = state.get("context_version")
        if isinstance(version, bool) or not isinstance(version, int) or version < 0:
            invalid_concurrency_state += 1
            continue
        lease_values = (
            state.get("stage_lease_token"),
            state.get("stage_lease_name"),
            state.get("stage_lease_expires_at"),
        )
        lease_is_clear = all(value is None for value in lease_values)
        lease_is_complete = all(value is not None for value in lease_values)
        if not lease_is_clear and not lease_is_complete:
            invalid_concurrency_state += 1
            continue
        if version > 0 or lease_is_complete:
            versioned_or_leased += 1
    if invalid_concurrency_state:
        raise MigrationError("existing project concurrency state is invalid")
    if versioned_or_leased:
        raise MigrationError(
            "existing project version or stage lease is nonzero; import refused"
        )

    rest.upsert("projects", target.projects, on_conflict="id", batch_size=batch_size)
    conversation_ids = rest.default_conversations(project_ids)
    message_rows = build_message_rows(target, conversation_ids, owner_id)

    for workflow in target.workflow_runs:
        workflow["conversation_id"] = conversation_ids[workflow["project_id"]]

    rest.upsert(
        "workflow_runs", target.workflow_runs, on_conflict="id", batch_size=batch_size
    )
    rest.upsert("messages", message_rows, on_conflict="id", batch_size=batch_size)
    rest.upsert("agent_runs", target.agent_runs, on_conflict="id", batch_size=batch_size)
    rest.upsert(
        "artifacts",
        target.artifacts,
        on_conflict="project_id,name",
        batch_size=batch_size,
    )
    rest.upsert("api_calls", target.api_calls, on_conflict="id", batch_size=batch_size)
    return message_rows


def print_counts_and_checksums(
    mode: str,
    inventory: Inventory,
    target: TargetRows,
    messages: Sequence[dict[str, Any]],
) -> None:
    print(f"mode.{mode}.count=1 sha256={checksum(mode)}")
    for key in sorted(inventory.counts):
        count = inventory.counts[key]
        print(f"{key}.count={count} sha256={checksum(count)}")
    for key in sorted(inventory.checksums):
        print(f"{key}.count=1 sha256={inventory.checksums[key]}")

    tables: dict[str, Sequence[dict[str, Any]]] = {
        "target.projects": target.projects,
        "target.workflow_runs": target.workflow_runs,
        "target.agent_runs": target.agent_runs,
        "target.artifacts": target.artifacts,
        "target.api_calls": target.api_calls,
        "target.messages": messages,
    }
    for label, rows in tables.items():
        ordered = sorted(rows, key=lambda row: str(row.get("id", "")))
        print(f"{label}.count={len(rows)} sha256={checksum(ordered)}")


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        owner_id, supabase_url, service_role_key = load_required_environment()
        fidelity_checks = run_fidelity_self_test()
        sources = (
            SourceSpec("legacy", args.legacy_data_dir.resolve(), 1),
            SourceSpec("current", args.current_data_dir.resolve(), 2),
        )
        inventory = load_inventory(sources, args.max_artifact_bytes)
        if not inventory.projects:
            raise MigrationError("no legacy projects were found")
        target = build_target_rows(inventory, owner_id)
        validation_conversations = placeholder_conversations(
            [row["id"] for row in target.projects]
        )
        validation_messages = build_message_rows(
            target,
            validation_conversations,
            owner_id,
        )
        structural_checks = validate_target_rows(
            target,
            validation_messages,
            owner_id,
        )
        inventory.counts["qa.canonical_fidelity_checks"] = fidelity_checks
        inventory.counts["qa.target_structural_checks"] = structural_checks

        if args.apply:
            rest = SupabaseRest(supabase_url, service_role_key, args.timeout_seconds)
            try:
                messages = apply_rows(
                    rest,
                    target,
                    owner_id,
                    batch_size=args.batch_size,
                    allow_owner_reassignment=args.allow_owner_reassignment,
                )
            finally:
                rest.close()
            if len(messages) != len(validation_messages):
                raise MigrationError("applied message count failed local validation")
            mode = "apply"
        else:
            messages = validation_messages
            mode = "dry_run"

        print_counts_and_checksums(mode, inventory, target, messages)
        return 0
    except MigrationError as exc:
        print(f"migration_error.count=1 sha256={checksum(str(exc))}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print(f"migration_cancelled.count=1 sha256={checksum('cancelled')}", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001 - prevent sensitive traceback output
        safe_category = type(exc).__name__
        print(
            f"migration_unexpected.count=1 sha256={checksum(safe_category)}",
            file=sys.stderr,
        )
        return 3


if __name__ == "__main__":
    raise SystemExit(main())
