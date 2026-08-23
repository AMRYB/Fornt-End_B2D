"""Tests for the SQLite-backed project store."""

from __future__ import annotations

import json

from agentic_core.project_store import ProjectStore
from agentic_core.schemas import ProjectContext


def test_create_save_load_round_trip(tmp_path):
    store = ProjectStore(tmp_path / "b2d.db")

    context = store.create("I want to build a booking platform.")
    context.target_users = ["Players", "Owners"]
    context.status = "ready_for_confirmation"
    store.save(context)

    loaded = store.load(context.project_id)
    assert loaded is not None
    assert loaded.business_idea == "I want to build a booking platform."
    assert loaded.target_users == ["Players", "Owners"]
    assert loaded.status == "ready_for_confirmation"


def test_load_missing_returns_none(tmp_path):
    store = ProjectStore(tmp_path / "b2d.db")
    assert store.load("nope") is None


def test_list_ids_and_overwrite(tmp_path):
    store = ProjectStore(tmp_path / "b2d.db")
    a = store.create("Idea A", project_id="proj_a")
    store.create("Idea B", project_id="proj_b")
    assert store.list_ids() == ["proj_a", "proj_b"]

    a.business_idea = "Idea A updated"
    store.save(a)
    assert store.load("proj_a").business_idea == "Idea A updated"


def test_store_reopens_existing_database(tmp_path):
    db = tmp_path / "b2d.db"
    ProjectStore(db).create("Persistent idea", project_id="proj_p")

    reopened = ProjectStore(db)
    assert reopened.load("proj_p").business_idea == "Persistent idea"


def test_migrates_legacy_json_files_once(tmp_path):
    legacy = tmp_path / "projects"
    legacy.mkdir()
    old = ProjectContext(project_id="proj_old", business_idea="Legacy idea")
    old.status = "approved"
    (legacy / "proj_old.json").write_text(
        json.dumps(old.model_dump(mode="json")), encoding="utf-8"
    )

    db = tmp_path / "b2d.db"
    store = ProjectStore(db, legacy_dir=legacy)
    assert store.load("proj_old").business_idea == "Legacy idea"

    # Idempotent: reopening does not duplicate or clobber newer state.
    ctx = store.load("proj_old")
    ctx.status = "needs_attention"
    store.save(ctx)
    ProjectStore(db, legacy_dir=legacy)
    assert store.load("proj_old").status == "needs_attention"