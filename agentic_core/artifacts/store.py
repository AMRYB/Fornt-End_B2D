"""Filesystem-backed artifact store under ``data/artifacts/<project_id>/``.

Writes are atomic: the new content is first written to a ``*.tmp`` sibling and
only renamed over the target after a successful write. A failed/interrupted
write can therefore never corrupt or destroy the previously generated artifact.
"""

from __future__ import annotations

import os
from pathlib import Path


class ArtifactStore:
    def __init__(self, artifacts_dir: Path):
        self._base = artifacts_dir
        self._base.mkdir(parents=True, exist_ok=True)

    def project_dir(self, project_id: str) -> Path:
        directory = self._base / _safe_name(project_id)
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def write(
        self,
        project_id: str,
        name: str,
        content: str,
        structured_data: dict | None = None,
    ) -> Path:
        del structured_data  # Structured outputs live in ProjectContext locally.
        directory = self.project_dir(project_id)
        target = directory / _safe_name(name)
        if target.parent != directory:
            raise ValueError("Artifact name escapes the project directory")
        # Atomic replace: write the temp file first, then rename. If the write
        # fails, the previous artifact remains untouched.
        tmp = directory / (target.name + ".tmp")
        try:
            tmp.write_text(content, encoding="utf-8")
            os.replace(tmp, target)
        finally:
            if tmp.exists():
                tmp.unlink(missing_ok=True)
        return target

    def list(self, project_id: str) -> list[str]:
        directory = self.project_dir(project_id)
        if not directory.exists():
            return []
        return sorted(path.name for path in directory.iterdir() if path.is_file())

    def read(self, project_id: str, name: str) -> str | None:
        target = self.project_dir(project_id) / _safe_name(name)
        if target.is_file():
            return target.read_text(encoding="utf-8")
        return None

    def list_metadata(self, project_id: str) -> list[dict]:
        directory = self.project_dir(project_id)
        rows: list[dict] = []
        for path in sorted(directory.iterdir(), key=lambda item: item.name):
            if not path.is_file():
                continue
            rows.append(
                {
                    "name": path.name,
                    "mime_type": "text/plain",
                    "byte_size": path.stat().st_size,
                    "sha256": None,
                    "created_at": None,
                    "updated_at": None,
                }
            )
        return rows


def _safe_name(name: str) -> str:
    cleaned = name.replace("\\", "/").split("/")[-1].strip()
    return cleaned or "_"
