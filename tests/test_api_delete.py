"""Contract tests for deleting a project (chat) and its derived state."""

from __future__ import annotations

import importlib

import httpx
import pytest

from agentic_core.artifacts import ArtifactStore
from agentic_core.config import get_settings


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


def _client(api_module):
    transport = httpx.ASGITransport(app=api_module.app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="https://app.example")


@pytest.mark.asyncio
async def test_delete_project_removes_project_artifacts_and_runs(api_module):
    services = api_module.services
    settings = services.settings
    project_id = "proj_del_full"
    services.project_store.create("Idea that will be deleted", project_id=project_id)
    services.artifact_store.write(project_id, "overview.md", "# Overview")
    services.tracker.start("requirements", project_id, {"probe": True})

    assert (settings.artifacts_dir / project_id / "overview.md").is_file()
    assert (settings.runs_dir / f"{project_id}.jsonl").is_file()

    async with _client(api_module) as client:
        response = await client.delete(
            f"/api/projects/{project_id}",
            headers={"Authorization": "Bearer browser-access-token"},
        )

    assert response.status_code == 204
    assert response.content == b""
    assert services.project_store.load(project_id) is None
    assert not (settings.artifacts_dir / project_id).exists()
    assert not (settings.runs_dir / f"{project_id}.jsonl").exists()


@pytest.mark.asyncio
async def test_delete_unknown_project_returns_404(api_module):
    async with _client(api_module) as client:
        response = await client.delete(
            "/api/projects/proj_missing",
            headers={"Authorization": "Bearer browser-access-token"},
        )

    assert response.status_code == 404
    assert response.json() == {"detail": "Project not found"}


@pytest.mark.asyncio
async def test_delete_twice_returns_404_the_second_time(api_module):
    services = api_module.services
    project_id = "proj_del_twice"
    services.project_store.create("Delete me once", project_id=project_id)

    async with _client(api_module) as client:
        first = await client.delete(
            f"/api/projects/{project_id}",
            headers={"Authorization": "Bearer browser-access-token"},
        )
        second = await client.delete(
            f"/api/projects/{project_id}",
            headers={"Authorization": "Bearer browser-access-token"},
        )

    assert first.status_code == 204
    assert second.status_code == 404


def test_artifact_delete_cannot_escape_the_artifacts_base(tmp_path):
    base = tmp_path / "artifacts"
    store = ArtifactStore(base)
    store.write("proj_ok", "overview.md", "# kept")
    store.write("..", "overview.md", "hostile")

    # Hostile identifiers resolve inside the base directory, never above it.
    assert (base / "_" / "overview.md").is_file()
    store.delete("../..")

    assert base.exists()
    assert (base / "proj_ok" / "overview.md").is_file()

    store.delete("proj_ok")
    assert not (base / "proj_ok").exists()
    assert base.exists()
