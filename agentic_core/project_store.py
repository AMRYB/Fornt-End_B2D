"""SQLite-backed persistence for projects.

Projects are stored as JSON blobs in a single SQLite database file
(``data/b2d.db``). The public API matches the previous JSON-file store, so the
rest of the system is unchanged: ``create``, ``save``, ``load``, ``list_ids``.
"""

from __future__ import annotations

import sqlite3
import uuid
from pathlib import Path

from .schemas import ProjectContext

_SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    project_id TEXT PRIMARY KEY,
    data TEXT NOT NULL,
    status TEXT,
    updated_at TEXT
);
"""


class ProjectStore:
    def __init__(self, db_path: Path, legacy_dir: Path | None = None):
        self._path = Path(db_path)
        self._path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()
        if legacy_dir is not None:
            self._migrate_legacy(legacy_dir)

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(_SCHEMA)

    def _migrate_legacy(self, legacy_dir: Path) -> None:
        """One-time import of projects saved as JSON files by the old store."""
        legacy_dir = Path(legacy_dir)
        if not legacy_dir.is_dir():
            return
        with self._connect() as conn:
            existing = {
                row["project_id"]
                for row in conn.execute("SELECT project_id FROM projects").fetchall()
            }
            for path in sorted(legacy_dir.glob("*.json")):
                try:
                    context = ProjectContext.model_validate_json(
                        path.read_text(encoding="utf-8")
                    )
                except Exception:
                    continue
                if context.project_id in existing:
                    continue
                conn.execute(
                    "INSERT INTO projects (project_id, data, status, updated_at) "
                    "VALUES (?, ?, ?, ?)",
                    (
                        context.project_id,
                        context.model_dump_json(indent=2),
                        context.status,
                        context.updated_at.isoformat(),
                    ),
                )
                existing.add(context.project_id)

    def create(
        self,
        business_idea: str,
        project_id: str | None = None,
        owner_id: str | None = None,
    ) -> ProjectContext:
        del owner_id  # Local development has a single implicit owner.
        project_id = project_id or f"proj_{uuid.uuid4().hex[:10]}"
        context = ProjectContext(project_id=project_id, business_idea=business_idea)
        self.save(context)
        return context

    def save(self, context: ProjectContext, owner_id: str | None = None) -> None:
        del owner_id
        with self._connect() as conn:
            conn.execute(
                "INSERT INTO projects (project_id, data, status, updated_at) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(project_id) DO UPDATE SET "
                "data = excluded.data, status = excluded.status, "
                "updated_at = excluded.updated_at",
                (
                    context.project_id,
                    context.model_dump_json(indent=2),
                    context.status,
                    context.updated_at.isoformat(),
                ),
            )

    def load(
        self, project_id: str, owner_id: str | None = None
    ) -> ProjectContext | None:
        del owner_id
        with self._connect() as conn:
            row = conn.execute(
                "SELECT data FROM projects WHERE project_id = ?", (project_id,)
            ).fetchone()
        if row is None:
            return None
        return ProjectContext.model_validate_json(row["data"])

    def list_ids(self, owner_id: str | None = None) -> list[str]:
        del owner_id
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT project_id FROM projects ORDER BY project_id"
            ).fetchall()
        return [row["project_id"] for row in rows]

    def list_projects(self, owner_id: str | None = None) -> list[dict]:
        del owner_id
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT project_id, data, status, updated_at FROM projects "
                "ORDER BY updated_at DESC"
            ).fetchall()
        projects: list[dict] = []
        for row in rows:
            try:
                context = ProjectContext.model_validate_json(row["data"])
            except Exception:
                continue
            projects.append(
                {
                    "project_id": row["project_id"],
                    "title": " ".join(context.business_idea.strip().split())[:80]
                    or "Untitled project",
                    "business_idea": context.business_idea,
                    "status": row["status"] or context.status,
                    "created_at": None,
                    "updated_at": row["updated_at"],
                }
            )
        return projects

    def delete(self, project_id: str, owner_id: str | None = None) -> None:
        del owner_id
        with self._connect() as conn:
            conn.execute("DELETE FROM projects WHERE project_id = ?", (project_id,))

    @staticmethod
    def sync_transcript(
        context: ProjectContext, owner_id: str | None = None
    ) -> None:
        # The complete transcript is already part of the local JSON blob.
        del context, owner_id
