"""Execution tracking: per-agent run history persisted as JSONL.

Stored in ``data/runs/<project_id>.jsonl`` for debugging, auditability and
demo visibility. No secrets are ever written here.
"""

from __future__ import annotations

import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from ..agents import AgentResult


class RunRecord(BaseModel):
    record_id: str = ""
    workflow_run_id: str | None = None
    project_id: str
    agent: str
    status: str
    input: dict[str, Any] | None = None
    output: dict[str, Any] | None = None
    error: str | None = None
    started_at: datetime = Field(default_factory=datetime.now)
    completed_at: datetime | None = None
    duration_ms: int | None = None
    retry_count: int = 0
    input_chars: int = 0
    output_chars: int = 0
    # Per-call LLM telemetry (see AgentResult).
    call_id: str = ""
    provider: str = ""
    model: str = ""
    ttft_s: float = 0.0
    total_s: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    total_tokens: int = 0


class ExecutionTracker:
    def __init__(self, runs_dir: Path):
        self._dir = runs_dir
        self._dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()

    def start(self, agent: str, project_id: str, input_: dict[str, Any] | None) -> RunRecord:
        record = RunRecord(project_id=project_id, agent=agent, status="started", input=input_)
        self._append(record)
        return record

    def complete(self, record: RunRecord, result: AgentResult) -> None:
        record.status = result.status
        record.output = result.output
        record.error = result.error
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
        record.completed_at = datetime.now()
        self._append(record)

    def list(self, project_id: str) -> list[RunRecord]:
        path = self._path(project_id)
        if not path.exists():
            return []
        records: list[RunRecord] = []
        with self._lock:
            lines = path.read_text(encoding="utf-8").splitlines()
        for line in lines:
            if line.strip():
                records.append(RunRecord.model_validate_json(line))
        return records

    def _path(self, project_id: str) -> Path:
        return self._dir / f"{project_id}.jsonl"

    def _append(self, record: RunRecord) -> None:
        with self._lock:
            with self._path(record.project_id).open("a", encoding="utf-8") as handle:
                handle.write(record.model_dump_json() + "\n")
