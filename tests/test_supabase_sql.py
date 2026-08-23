from __future__ import annotations

from pathlib import Path


MIGRATION = (
    Path(__file__).resolve().parents[1]
    / "supabase"
    / "migrations"
    / "202608220001_initial_b2d.sql"
)


def test_migration_contains_atomic_quota_and_stage_claims():
    sql = MIGRATION.read_text(encoding="utf-8").lower()

    assert "create table if not exists public.usage_daily" in sql
    assert "create table if not exists public.usage_claims" in sql
    assert "create or replace function public.claim_user_quota" in sql
    assert "message = 'daily_quota_exceeded'" in sql
    assert "p_idempotency_key uuid" in sql
    assert "p_fingerprint text" in sql
    assert "fingerprint ~ '^[0-9a-f]{64}$'" in sql
    assert "on conflict (user_id, usage_date, kind, idempotency_key) do nothing" in sql
    assert "v_existing_fingerprint is distinct from p_fingerprint" in sql
    assert "message = 'quota_idempotency_conflict'" in sql
    assert "create or replace function public.claim_generation_stage" in sql
    assert "create or replace function public.claim_generation_stage_idempotent" in sql
    assert "create or replace function public.commit_generation_stage" in sql
    assert "error = '{}'::jsonb" in sql
    assert "grant execute on function public.claim_user_quota" in sql
    assert "claim_user_quota(uuid, text, integer, uuid, text)" in sql
    assert "to service_role" in sql


def test_browser_roles_have_no_application_mutation_grants():
    sql = MIGRATION.read_text(encoding="utf-8").lower()

    protected = (
        "projects",
        "conversations",
        "messages",
        "workflow_runs",
        "agent_runs",
        "artifacts",
        "api_calls",
        "usage_daily",
        "usage_claims",
    )
    for table in protected:
        assert f"grant insert on table public.{table} to authenticated" not in sql
        assert f"grant delete on table public.{table} to authenticated" not in sql
        assert f"grant update on table public.{table} to authenticated" not in sql


def test_project_identity_and_version_are_database_enforced():
    sql = MIGRATION.read_text(encoding="utf-8").lower()

    assert "context_version bigint not null default 0" in sql
    assert "context ->> 'project_id' = id" in sql
    assert "stage_lease_token uuid" in sql
    assert "stage_lease_expires_at timestamptz" in sql
    assert "p.generation_state ? 'discovery_lease'" in sql
