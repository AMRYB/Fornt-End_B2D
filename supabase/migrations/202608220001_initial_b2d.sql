-- Business to Development: production persistence schema for Supabase.
--
-- This migration deliberately keeps Gemini/provider credentials out of the
-- database. Browser clients may read their own generated data, but every
-- application-data mutation is performed by the authenticated trusted backend
-- (service_role). Browser profile edits are limited to three safe columns.

begin;

create extension if not exists pgcrypto with schema extensions;

-- ---------------------------------------------------------------------------
-- Shared trigger helpers

create or replace function public.set_updated_at()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  new.updated_at := timezone('utc', now());
  return new;
end;
$$;

revoke all on function public.set_updated_at() from public;

-- ---------------------------------------------------------------------------
-- Accounts. Passwords, sessions, MFA, and email confirmation remain entirely
-- in Supabase Auth; this table only stores application-facing profile data.

create table if not exists public.profiles (
  id uuid primary key references auth.users(id) on delete cascade,
  email text,
  display_name text,
  avatar_url text,
  preferences jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default timezone('utc', now()),
  updated_at timestamptz not null default timezone('utc', now()),
  constraint profiles_preferences_object
    check (jsonb_typeof(preferences) = 'object')
);

create or replace function public.handle_new_auth_user()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  insert into public.profiles (id, email, display_name, avatar_url)
  values (
    new.id,
    new.email,
    coalesce(
      nullif(new.raw_user_meta_data ->> 'full_name', ''),
      nullif(new.raw_user_meta_data ->> 'name', ''),
      nullif(split_part(coalesce(new.email, ''), '@', 1), ''),
      'User'
    ),
    nullif(new.raw_user_meta_data ->> 'avatar_url', '')
  )
  on conflict (id) do update
    set email = excluded.email,
        updated_at = timezone('utc', now());
  return new;
end;
$$;

revoke all on function public.handle_new_auth_user() from public;

drop trigger if exists on_auth_user_created on auth.users;
create trigger on_auth_user_created
after insert on auth.users
for each row execute function public.handle_new_auth_user();

-- Backfill profiles when this migration is applied to a project that already
-- has Auth users. Existing user-edited profile fields are intentionally kept.
insert into public.profiles (id, email, display_name, avatar_url)
select
  u.id,
  u.email,
  coalesce(
    nullif(u.raw_user_meta_data ->> 'full_name', ''),
    nullif(u.raw_user_meta_data ->> 'name', ''),
    nullif(split_part(coalesce(u.email, ''), '@', 1), ''),
    'User'
  ),
  nullif(u.raw_user_meta_data ->> 'avatar_url', '')
from auth.users as u
on conflict (id) do nothing;

-- ---------------------------------------------------------------------------
-- Projects and conversations

create table if not exists public.projects (
  id text primary key,
  user_id uuid not null references auth.users(id) on delete cascade,
  business_idea text not null,
  title text not null default 'Untitled project',
  status text not null default 'discovery',
  context jsonb not null default '{}'::jsonb,
  generation_state jsonb not null default '{}'::jsonb,
  context_version bigint not null default 0,
  stage_lease_token uuid,
  stage_lease_name text,
  stage_lease_expires_at timestamptz,
  created_at timestamptz not null default timezone('utc', now()),
  updated_at timestamptz not null default timezone('utc', now()),
  constraint projects_id_not_blank check (length(btrim(id)) between 1 and 128),
  constraint projects_business_idea_not_blank check (length(btrim(business_idea)) > 0),
  constraint projects_status_valid check (
    status in (
      'discovery',
      'ready_for_confirmation',
      'confirmed',
      'generating',
      'approved',
      'revised',
      'needs_attention'
    )
  ),
  constraint projects_context_object check (jsonb_typeof(context) = 'object'),
  constraint projects_generation_state_object
    check (jsonb_typeof(generation_state) = 'object'),
  constraint projects_context_version_nonnegative check (context_version >= 0),
  constraint projects_context_identity_matches check (
    context ? 'project_id' and context ->> 'project_id' = id
  ),
  constraint projects_stage_lease_name_not_blank check (
    stage_lease_name is null or length(btrim(stage_lease_name)) > 0
  ),
  constraint projects_stage_lease_complete check (
    (
      stage_lease_token is null
      and stage_lease_name is null
      and stage_lease_expires_at is null
    )
    or
    (
      stage_lease_token is not null
      and stage_lease_name is not null
      and stage_lease_expires_at is not null
    )
  )
);

-- These additions also make a re-run upgrade an earlier copy of this same
-- migration that predated optimistic concurrency and stage leases.
alter table public.projects
  add column if not exists context_version bigint not null default 0,
  add column if not exists stage_lease_token uuid,
  add column if not exists stage_lease_name text,
  add column if not exists stage_lease_expires_at timestamptz;

do $migration$
begin
  if not exists (
    select 1 from pg_catalog.pg_constraint
    where conrelid = 'public.projects'::regclass
      and conname = 'projects_context_version_nonnegative'
  ) then
    alter table public.projects
      add constraint projects_context_version_nonnegative
      check (context_version >= 0);
  end if;
  if not exists (
    select 1 from pg_catalog.pg_constraint
    where conrelid = 'public.projects'::regclass
      and conname = 'projects_context_identity_matches'
  ) then
    alter table public.projects
      add constraint projects_context_identity_matches
      check (context ? 'project_id' and context ->> 'project_id' = id);
  end if;
  if not exists (
    select 1 from pg_catalog.pg_constraint
    where conrelid = 'public.projects'::regclass
      and conname = 'projects_stage_lease_name_not_blank'
  ) then
    alter table public.projects
      add constraint projects_stage_lease_name_not_blank
      check (stage_lease_name is null or length(btrim(stage_lease_name)) > 0);
  end if;
  if not exists (
    select 1 from pg_catalog.pg_constraint
    where conrelid = 'public.projects'::regclass
      and conname = 'projects_stage_lease_complete'
  ) then
    alter table public.projects
      add constraint projects_stage_lease_complete
      check (
        (
          stage_lease_token is null
          and stage_lease_name is null
          and stage_lease_expires_at is null
        )
        or
        (
          stage_lease_token is not null
          and stage_lease_name is not null
          and stage_lease_expires_at is not null
        )
      );
  end if;
end;
$migration$;

create table if not exists public.conversations (
  id uuid primary key default gen_random_uuid(),
  project_id text not null references public.projects(id) on delete cascade,
  user_id uuid not null references auth.users(id) on delete cascade,
  parent_conversation_id uuid references public.conversations(id) on delete set null,
  title text not null default 'Project conversation',
  is_default boolean not null default false,
  is_pinned boolean not null default false,
  is_archived boolean not null default false,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default timezone('utc', now()),
  updated_at timestamptz not null default timezone('utc', now()),
  constraint conversations_title_not_blank check (length(btrim(title)) > 0),
  constraint conversations_metadata_object check (jsonb_typeof(metadata) = 'object'),
  constraint conversations_id_project_unique unique (id, project_id)
);

create unique index if not exists conversations_one_default_per_project_idx
  on public.conversations (project_id)
  where is_default;

create or replace function public.ensure_default_project_conversation()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  insert into public.conversations (project_id, user_id, title, is_default)
  values (new.id, new.user_id, 'Project conversation', true)
  on conflict (project_id) where is_default do nothing;
  return new;
end;
$$;

revoke all on function public.ensure_default_project_conversation() from public;

drop trigger if exists projects_create_default_conversation on public.projects;
create trigger projects_create_default_conversation
after insert on public.projects
for each row execute function public.ensure_default_project_conversation();

-- Backfill a default conversation for projects that existed before the trigger.
insert into public.conversations (project_id, user_id, title, is_default)
select p.id, p.user_id, 'Project conversation', true
from public.projects as p
where not exists (
  select 1
  from public.conversations as c
  where c.project_id = p.id and c.is_default
)
on conflict (project_id) where is_default do nothing;

-- Keep the denormalized conversation owner aligned with the project. The
-- backend filters on both fields, while RLS continues to trust projects as the
-- source of ownership truth.
create or replace function public.enforce_conversation_owner()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  select p.user_id
    into new.user_id
    from public.projects as p
    where p.id = new.project_id;
  if new.user_id is null then
    raise foreign_key_violation using message = 'conversation project does not exist';
  end if;
  return new;
end;
$$;

revoke all on function public.enforce_conversation_owner() from public;

drop trigger if exists conversations_enforce_owner on public.conversations;
create trigger conversations_enforce_owner
before insert or update of project_id, user_id on public.conversations
for each row execute function public.enforce_conversation_owner();

create or replace function public.sync_project_conversation_owner()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  if new.user_id is distinct from old.user_id then
    update public.conversations
      set user_id = new.user_id
      where project_id = new.id;
  end if;
  return new;
end;
$$;

revoke all on function public.sync_project_conversation_owner() from public;

drop trigger if exists projects_sync_conversation_owner on public.projects;
create trigger projects_sync_conversation_owner
after update of user_id on public.projects
for each row execute function public.sync_project_conversation_owner();

create table if not exists public.messages (
  id uuid primary key default gen_random_uuid(),
  conversation_id uuid not null,
  project_id text not null references public.projects(id) on delete cascade,
  sender_user_id uuid references auth.users(id) on delete set null,
  role text not null,
  -- The sentinel default lets PostgREST omit this server-managed column. The
  -- BEFORE INSERT trigger replaces it before constraints are evaluated.
  turn_index bigint not null default -1,
  content text not null default '',
  structured_data jsonb not null default '{}'::jsonb,
  metadata jsonb not null default '{}'::jsonb,
  client_message_id uuid,
  created_at timestamptz not null default timezone('utc', now()),
  constraint messages_role_valid
    check (role in ('user', 'assistant', 'system', 'agent', 'tool')),
  constraint messages_role_author_valid check (
    (role = 'user' and sender_user_id is not null)
    or (role <> 'user' and sender_user_id is null)
  ),
  constraint messages_user_content_not_blank
    check (role <> 'user' or length(btrim(content)) > 0),
  constraint messages_structured_data_object
    check (jsonb_typeof(structured_data) = 'object'),
  constraint messages_metadata_object check (jsonb_typeof(metadata) = 'object'),
  constraint messages_turn_index_nonnegative check (turn_index >= 0),
  constraint messages_conversation_project_fk
    foreign key (conversation_id, project_id)
    references public.conversations(id, project_id)
    on delete cascade,
  constraint messages_conversation_turn_unique unique (conversation_id, turn_index)
);

create unique index if not exists messages_client_id_unique_idx
  on public.messages (conversation_id, client_message_id)
  where client_message_id is not null;

-- A transaction-level advisory lock prevents two concurrent inserts from
-- receiving the same turn index. Trusted imports may provide an explicit index.
create or replace function public.assign_message_turn_index()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  if new.turn_index is null or new.turn_index < 0 then
    perform pg_catalog.pg_advisory_xact_lock(
      pg_catalog.hashtextextended(new.conversation_id::text, 0)
    );
    select coalesce(max(m.turn_index) + 1, 0)
      into new.turn_index
      from public.messages as m
      where m.conversation_id = new.conversation_id;
  end if;
  return new;
end;
$$;

revoke all on function public.assign_message_turn_index() from public;

drop trigger if exists messages_assign_turn_index on public.messages;
create trigger messages_assign_turn_index
before insert on public.messages
for each row execute function public.assign_message_turn_index();

-- ---------------------------------------------------------------------------
-- Workflow, agent, rendered artifact, and sanitized provider/API telemetry

create table if not exists public.workflow_runs (
  id uuid primary key default gen_random_uuid(),
  project_id text not null references public.projects(id) on delete cascade,
  conversation_id uuid references public.conversations(id) on delete set null,
  user_id uuid not null references auth.users(id) on delete cascade,
  status text not null default 'queued',
  current_stage text,
  idempotency_key text,
  context_snapshot jsonb not null default '{}'::jsonb,
  summary jsonb not null default '{}'::jsonb,
  error jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default timezone('utc', now()),
  started_at timestamptz,
  completed_at timestamptz,
  updated_at timestamptz not null default timezone('utc', now()),
  constraint workflow_runs_status_valid check (
    status in (
      'queued', 'running', 'approved', 'revised',
      'needs_attention', 'failed', 'cancelled'
    )
  ),
  constraint workflow_runs_context_snapshot_object
    check (jsonb_typeof(context_snapshot) = 'object'),
  constraint workflow_runs_summary_object check (jsonb_typeof(summary) = 'object'),
  constraint workflow_runs_error_object check (jsonb_typeof(error) = 'object'),
  constraint workflow_runs_time_order check (
    completed_at is null or started_at is null or completed_at >= started_at
  )
);

create unique index if not exists workflow_runs_one_active_per_project_idx
  on public.workflow_runs (project_id)
  where status in ('queued', 'running');

create unique index if not exists workflow_runs_idempotency_idx
  on public.workflow_runs (project_id, idempotency_key)
  where idempotency_key is not null;

create table if not exists public.agent_runs (
  id uuid primary key default gen_random_uuid(),
  workflow_run_id uuid references public.workflow_runs(id) on delete cascade,
  project_id text not null references public.projects(id) on delete cascade,
  agent text not null,
  invocation integer not null default 1,
  attempt integer not null default 0,
  status text not null default 'started',
  provider text,
  model text,
  external_call_id text,
  input_data jsonb default '{}'::jsonb,
  output_data jsonb default '{}'::jsonb,
  error text,
  telemetry jsonb not null default '{}'::jsonb,
  retry_count integer not null default 0,
  input_chars bigint not null default 0,
  output_chars bigint not null default 0,
  started_at timestamptz not null default timezone('utc', now()),
  completed_at timestamptz,
  duration_ms bigint,
  created_at timestamptz not null default timezone('utc', now()),
  updated_at timestamptz not null default timezone('utc', now()),
  constraint agent_runs_agent_valid check (
    agent in (
      'discovery', 'requirements', 'architecture', 'database',
      'api', 'devops', 'reviewer'
    )
  ),
  constraint agent_runs_status_valid
    check (status in ('started', 'retrying', 'success', 'failed', 'cancelled')),
  constraint agent_runs_invocation_positive check (invocation > 0),
  constraint agent_runs_attempt_nonnegative check (attempt >= 0),
  constraint agent_runs_retry_count_nonnegative check (retry_count >= 0),
  constraint agent_runs_input_chars_nonnegative check (input_chars >= 0),
  constraint agent_runs_output_chars_nonnegative check (output_chars >= 0),
  constraint agent_runs_duration_nonnegative check (duration_ms is null or duration_ms >= 0),
  constraint agent_runs_input_object
    check (input_data is null or jsonb_typeof(input_data) = 'object'),
  constraint agent_runs_output_object
    check (output_data is null or jsonb_typeof(output_data) = 'object'),
  constraint agent_runs_telemetry_object check (jsonb_typeof(telemetry) = 'object'),
  constraint agent_runs_time_order check (
    completed_at is null or completed_at >= started_at
  )
);

create table if not exists public.artifacts (
  id uuid primary key default gen_random_uuid(),
  project_id text not null references public.projects(id) on delete cascade,
  workflow_run_id uuid references public.workflow_runs(id) on delete set null,
  agent_run_id uuid references public.agent_runs(id) on delete set null,
  name text not null,
  artifact_type text not null default 'file',
  content_text text,
  structured_data jsonb not null default '{}'::jsonb,
  mime_type text not null default 'text/plain',
  byte_size bigint not null default 0,
  sha256 text,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default timezone('utc', now()),
  updated_at timestamptz not null default timezone('utc', now()),
  constraint artifacts_name_not_blank check (length(btrim(name)) > 0),
  constraint artifacts_type_not_blank check (length(btrim(artifact_type)) > 0),
  constraint artifacts_structured_data_object
    check (jsonb_typeof(structured_data) = 'object'),
  constraint artifacts_metadata_object check (jsonb_typeof(metadata) = 'object'),
  constraint artifacts_byte_size_nonnegative check (byte_size >= 0),
  constraint artifacts_sha256_valid check (
    sha256 is null or sha256 ~ '^[0-9a-f]{64}$'
  ),
  constraint artifacts_project_name_unique unique (project_id, name)
);

create table if not exists public.api_calls (
  id uuid primary key default gen_random_uuid(),
  project_id text not null references public.projects(id) on delete cascade,
  workflow_run_id uuid references public.workflow_runs(id) on delete cascade,
  agent_run_id uuid references public.agent_runs(id) on delete cascade,
  provider text not null,
  operation text not null,
  status text not null,
  external_call_id text,
  http_status integer,
  request_metadata jsonb not null default '{}'::jsonb,
  response_metadata jsonb not null default '{}'::jsonb,
  telemetry jsonb not null default '{}'::jsonb,
  error jsonb not null default '{}'::jsonb,
  sanitized boolean not null default true,
  started_at timestamptz,
  completed_at timestamptz,
  duration_ms bigint,
  created_at timestamptz not null default timezone('utc', now()),
  updated_at timestamptz not null default timezone('utc', now()),
  constraint api_calls_status_valid check (status in ('started', 'success', 'failed')),
  constraint api_calls_http_status_valid check (
    http_status is null or http_status between 100 and 599
  ),
  constraint api_calls_duration_nonnegative check (duration_ms is null or duration_ms >= 0),
  constraint api_calls_request_metadata_object
    check (jsonb_typeof(request_metadata) = 'object'),
  constraint api_calls_response_metadata_object
    check (jsonb_typeof(response_metadata) = 'object'),
  constraint api_calls_telemetry_object check (jsonb_typeof(telemetry) = 'object'),
  constraint api_calls_error_object check (jsonb_typeof(error) = 'object'),
  constraint api_calls_must_be_sanitized check (sanitized),
  constraint api_calls_time_order check (
    completed_at is null or started_at is null or completed_at >= started_at
  )
);

-- Server-side daily counters protect the shared Gemini credentials from one
-- account creating unbounded paid/free-tier traffic. Browser roles can only
-- read their own counters; only the backend service role may claim quota.
create table if not exists public.usage_daily (
  user_id uuid not null references auth.users(id) on delete cascade,
  usage_date date not null default (timezone('utc', now()))::date,
  project_creates integer not null default 0,
  discovery_calls integer not null default 0,
  generation_stages integer not null default 0,
  total_calls integer not null default 0,
  primary key (user_id, usage_date),
  constraint usage_daily_project_creates_nonnegative check (project_creates >= 0),
  constraint usage_daily_discovery_calls_nonnegative check (discovery_calls >= 0),
  constraint usage_daily_generation_stages_nonnegative check (generation_stages >= 0),
  constraint usage_daily_total_calls_nonnegative check (total_calls >= 0)
);

-- One logical browser operation consumes quota at most once, even when a
-- serverless/PostgREST response is lost and the same Idempotency-Key is
-- replayed. Rows are naturally removed with the owning daily counter.
create table if not exists public.usage_claims (
  user_id uuid not null,
  usage_date date not null,
  kind text not null,
  idempotency_key uuid not null,
  fingerprint text not null,
  created_at timestamptz not null default timezone('utc', now()),
  primary key (user_id, usage_date, kind, idempotency_key),
  constraint usage_claims_kind_valid check (
    kind in ('project_create', 'discovery', 'generation_stage')
  ),
  constraint usage_claims_fingerprint_valid check (
    fingerprint ~ '^[0-9a-f]{64}$'
  ),
  constraint usage_claims_daily_fk
    foreign key (user_id, usage_date)
    references public.usage_daily(user_id, usage_date)
    on delete cascade
);

-- Keep this initial migration safe to re-run against an environment created by
-- an earlier pre-release schema. Legacy claims fail closed for the remainder
-- of their UTC day: clients must use a new key rather than silently attaching
-- different request semantics to an old quota receipt.
alter table public.usage_claims
  add column if not exists fingerprint text;
update public.usage_claims
   set fingerprint = repeat('0', 64)
 where fingerprint is null;
alter table public.usage_claims
  alter column fingerprint set not null;

do $migration$
begin
  if not exists (
    select 1
    from pg_catalog.pg_constraint
    where conrelid = 'public.usage_claims'::regclass
      and conname = 'usage_claims_fingerprint_valid'
  ) then
    alter table public.usage_claims
      add constraint usage_claims_fingerprint_valid
      check (fingerprint ~ '^[0-9a-f]{64}$');
  end if;
end;
$migration$;

-- ---------------------------------------------------------------------------
-- Query indexes

create index if not exists projects_user_updated_idx
  on public.projects (user_id, updated_at desc);
create index if not exists projects_user_status_idx
  on public.projects (user_id, status);
create index if not exists projects_expiring_stage_lease_idx
  on public.projects (stage_lease_expires_at)
  where stage_lease_token is not null;
create index if not exists conversations_project_updated_idx
  on public.conversations (project_id, updated_at desc);
create index if not exists conversations_user_updated_idx
  on public.conversations (user_id, updated_at desc);
create index if not exists conversations_parent_idx
  on public.conversations (parent_conversation_id)
  where parent_conversation_id is not null;
create index if not exists messages_conversation_created_idx
  on public.messages (conversation_id, created_at);
create index if not exists messages_user_idx
  on public.messages (sender_user_id)
  where sender_user_id is not null;
create index if not exists messages_project_created_idx
  on public.messages (project_id, created_at);
create index if not exists workflow_runs_project_created_idx
  on public.workflow_runs (project_id, created_at desc);
create index if not exists workflow_runs_user_created_idx
  on public.workflow_runs (user_id, created_at desc);
create index if not exists workflow_runs_conversation_idx
  on public.workflow_runs (conversation_id)
  where conversation_id is not null;
create index if not exists agent_runs_workflow_agent_idx
  on public.agent_runs (workflow_run_id, agent, invocation);
create index if not exists agent_runs_project_created_idx
  on public.agent_runs (project_id, created_at desc);
create index if not exists artifacts_project_type_idx
  on public.artifacts (project_id, artifact_type, updated_at desc);
create index if not exists artifacts_workflow_idx
  on public.artifacts (workflow_run_id)
  where workflow_run_id is not null;
create index if not exists api_calls_project_created_idx
  on public.api_calls (project_id, created_at desc);
create index if not exists api_calls_agent_run_idx
  on public.api_calls (agent_run_id)
  where agent_run_id is not null;
create index if not exists api_calls_external_call_idx
  on public.api_calls (external_call_id)
  where external_call_id is not null;

-- updated_at is server-managed on every mutable backend row.
drop trigger if exists profiles_set_updated_at on public.profiles;
create trigger profiles_set_updated_at
before update on public.profiles
for each row execute function public.set_updated_at();

drop trigger if exists projects_set_updated_at on public.projects;
create trigger projects_set_updated_at
before update on public.projects
for each row execute function public.set_updated_at();

drop trigger if exists conversations_set_updated_at on public.conversations;
create trigger conversations_set_updated_at
before update on public.conversations
for each row execute function public.set_updated_at();

drop trigger if exists workflow_runs_set_updated_at on public.workflow_runs;
create trigger workflow_runs_set_updated_at
before update on public.workflow_runs
for each row execute function public.set_updated_at();

drop trigger if exists agent_runs_set_updated_at on public.agent_runs;
create trigger agent_runs_set_updated_at
before update on public.agent_runs
for each row execute function public.set_updated_at();

drop trigger if exists artifacts_set_updated_at on public.artifacts;
create trigger artifacts_set_updated_at
before update on public.artifacts
for each row execute function public.set_updated_at();

drop trigger if exists api_calls_set_updated_at on public.api_calls;
create trigger api_calls_set_updated_at
before update on public.api_calls
for each row execute function public.set_updated_at();

-- ---------------------------------------------------------------------------
-- Service-only optimistic-concurrency and generation-stage lease RPCs.
--
-- All write paths compare an exact owner and context version. A stage worker
-- must claim a named stage, use the returned version/token for its checkpoint,
-- and either commit or release that exact lease. No function falls back to the
-- requirements stage when generation_state.next_stage is null.

create or replace function public.save_project_context(
  p_project_id text,
  p_user_id uuid,
  p_expected_version bigint,
  p_business_idea text,
  p_title text,
  p_status text,
  p_context jsonb,
  p_generation_state jsonb
)
returns bigint
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_new_version bigint;
  v_now timestamptz := clock_timestamp();
begin
  if p_project_id is null
     or length(btrim(p_project_id)) = 0
     or p_expected_version is null
     or p_expected_version < 0
     or p_user_id is null
     or length(btrim(coalesce(p_business_idea, ''))) = 0
     or length(btrim(coalesce(p_title, ''))) = 0
     or p_status is null
     or p_status not in (
       'discovery', 'ready_for_confirmation', 'confirmed', 'generating',
       'approved', 'revised', 'needs_attention'
     )
     or jsonb_typeof(p_context) is distinct from 'object'
     or p_context ->> 'project_id' is distinct from p_project_id
     or jsonb_typeof(p_generation_state) is distinct from 'object'
  then
    raise exception using
      errcode = '22023',
      message = 'invalid_project_payload';
  end if;

  update public.projects as p
     set business_idea = p_business_idea,
         title = p_title,
         status = p_status,
         context = p_context,
         generation_state = p_generation_state,
         context_version = p.context_version + 1,
         stage_lease_token = null,
         stage_lease_name = null,
         stage_lease_expires_at = null,
         updated_at = v_now
   where p.id = p_project_id
     and p.user_id = p_user_id
     and p.context_version = p_expected_version
     -- Confirmation must never overtake a Discovery call that already owns
     -- the optimistic checkpoint. The API first removes an expired receipt;
     -- a live receipt therefore forces a retry instead of wasting Gemini work.
     and not (
       p_status = 'confirmed'
       and p.status = 'ready_for_confirmation'
       and p.generation_state ? 'discovery_lease'
     )
     and (
       p.stage_lease_token is null
       or p.stage_lease_expires_at <= v_now
     )
  returning p.context_version into v_new_version;

  if not found then
    raise exception using
      errcode = '40001',
      message = 'project_write_conflict';
  end if;
  return v_new_version;
end;
$$;

create or replace function public.claim_generation_stage(
  p_project_id text,
  p_user_id uuid,
  p_expected_version bigint,
  p_expected_stage text,
  p_lease_seconds integer default 270
)
returns table (lease_token uuid, context_version bigint)
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_token uuid := gen_random_uuid();
  v_now timestamptz := clock_timestamp();
begin
  if p_project_id is null
     or length(btrim(p_project_id)) = 0
     or p_expected_version is null
     or p_expected_version < 0
     or p_user_id is null
     or length(btrim(coalesce(p_expected_stage, ''))) = 0
     or p_lease_seconds is null
     or p_lease_seconds < 30
     or p_lease_seconds > 900
  then
    raise exception using
      errcode = '22023',
      message = 'invalid_stage_claim';
  end if;

  update public.projects as p
     set stage_lease_token = v_token,
         stage_lease_name = p_expected_stage,
         stage_lease_expires_at = v_now + make_interval(secs => p_lease_seconds),
         context_version = p.context_version + 1,
         updated_at = v_now
   where p.id = p_project_id
     and p.user_id = p_user_id
     and p.context_version = p_expected_version
     and p.status = 'generating'
     and p.generation_state ->> 'next_stage' = p_expected_stage
     and (
       p.stage_lease_token is null
       or p.stage_lease_expires_at <= v_now
     )
  returning p.stage_lease_token, p.context_version
       into lease_token, context_version;

  if not found then
    raise exception using
      errcode = '40001',
      message = 'generation_stage_conflict';
  end if;
  return next;
end;
$$;

-- Idempotent variant used by the serverless API. The caller supplies the
-- token, so a lost HTTP response can be reconciled by replaying the same RPC
-- once without claiming or billing the stage twice.
create or replace function public.claim_generation_stage_idempotent(
  p_project_id text,
  p_user_id uuid,
  p_expected_version bigint,
  p_expected_stage text,
  p_lease_token uuid,
  p_lease_seconds integer default 240
)
returns table (lease_token uuid, context_version bigint)
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_now timestamptz := clock_timestamp();
begin
  if p_project_id is null
     or length(btrim(p_project_id)) = 0
     or p_expected_version is null
     or p_expected_version < 0
     or p_user_id is null
     or p_lease_token is null
     or length(btrim(coalesce(p_expected_stage, ''))) = 0
     or p_lease_seconds is null
     or p_lease_seconds < 30
     or p_lease_seconds > 900
  then
    raise exception using
      errcode = '22023',
      message = 'invalid_stage_claim';
  end if;

  -- Reconcile an earlier successful UPDATE whose HTTP response was lost.
  select p.stage_lease_token, p.context_version
    into lease_token, context_version
    from public.projects as p
   where p.id = p_project_id
     and p.user_id = p_user_id
     and p.status = 'generating'
     and p.generation_state ->> 'next_stage' = p_expected_stage
     and p.stage_lease_token = p_lease_token
     and p.stage_lease_name = p_expected_stage
     and p.stage_lease_expires_at > v_now
     and p.context_version = p_expected_version + 1;

  if found then
    return next;
    return;
  end if;

  update public.projects as p
     set stage_lease_token = p_lease_token,
         stage_lease_name = p_expected_stage,
         stage_lease_expires_at = v_now + make_interval(secs => p_lease_seconds),
         context_version = p.context_version + 1,
         updated_at = v_now
   where p.id = p_project_id
     and p.user_id = p_user_id
     and p.context_version = p_expected_version
     and p.status = 'generating'
     and p.generation_state ->> 'next_stage' = p_expected_stage
     and (
       p.stage_lease_token is null
       or p.stage_lease_expires_at <= v_now
     )
  returning p.stage_lease_token, p.context_version
       into lease_token, context_version;

  if not found then
    raise exception using
      errcode = '40001',
      message = 'generation_stage_conflict';
  end if;
  return next;
end;
$$;

create or replace function public.commit_generation_stage(
  p_project_id text,
  p_user_id uuid,
  p_expected_version bigint,
  p_expected_stage text,
  p_lease_token uuid,
  p_business_idea text,
  p_title text,
  p_status text,
  p_context jsonb,
  p_generation_state jsonb,
  p_workflow_run_id uuid default null,
  p_workflow_summary jsonb default '{}'::jsonb
)
returns bigint
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_new_version bigint;
  v_workflow_rows integer;
  v_now timestamptz := clock_timestamp();
  v_terminal boolean := p_status in ('approved', 'revised', 'needs_attention');
begin
  if p_project_id is null
     or length(btrim(p_project_id)) = 0
     or p_expected_version is null
     or p_expected_version < 0
     or p_user_id is null
     or p_lease_token is null
     or length(btrim(coalesce(p_expected_stage, ''))) = 0
     or length(btrim(coalesce(p_business_idea, ''))) = 0
     or length(btrim(coalesce(p_title, ''))) = 0
     or p_status is null
     or p_status not in ('generating', 'approved', 'revised', 'needs_attention')
     or jsonb_typeof(p_context) is distinct from 'object'
     or p_context ->> 'project_id' is distinct from p_project_id
     or jsonb_typeof(p_generation_state) is distinct from 'object'
     or jsonb_typeof(coalesce(p_workflow_summary, '{}'::jsonb)) is distinct from 'object'
  then
    raise exception using
      errcode = '22023',
      message = 'invalid_stage_commit';
  end if;

  update public.projects as p
     set business_idea = p_business_idea,
         title = p_title,
         status = p_status,
         context = p_context,
         generation_state = p_generation_state,
         context_version = p.context_version + 1,
         stage_lease_token = null,
         stage_lease_name = null,
         stage_lease_expires_at = null,
         updated_at = v_now
   where p.id = p_project_id
     and p.user_id = p_user_id
     and p.context_version = p_expected_version
     and p.status = 'generating'
     and p.generation_state ->> 'next_stage' = p_expected_stage
     and p.stage_lease_token = p_lease_token
     and p.stage_lease_name = p_expected_stage
     and p.stage_lease_expires_at > v_now
  returning p.context_version into v_new_version;

  if not found then
    raise exception using
      errcode = '40001',
      message = 'generation_stage_conflict';
  end if;

  if p_workflow_run_id is not null then
    update public.workflow_runs as w
       set status = case when v_terminal then p_status else 'running' end,
           current_stage = case
             when v_terminal then p_expected_stage
             else p_generation_state ->> 'next_stage'
           end,
           summary = coalesce(w.summary, '{}'::jsonb)
                     || coalesce(p_workflow_summary, '{}'::jsonb),
           error = '{}'::jsonb,
           started_at = coalesce(w.started_at, v_now),
           completed_at = case
             when v_terminal then coalesce(w.completed_at, v_now)
             else null
           end,
           updated_at = v_now
     where w.id = p_workflow_run_id
       and w.project_id = p_project_id
       and w.user_id = p_user_id;

    get diagnostics v_workflow_rows = row_count;
    if v_workflow_rows <> 1 then
      raise exception using
        errcode = '40001',
        message = 'workflow_run_conflict';
    end if;
  end if;

  return v_new_version;
end;
$$;

create or replace function public.release_generation_stage(
  p_project_id text,
  p_user_id uuid,
  p_lease_token uuid,
  p_error jsonb default '{}'::jsonb
)
returns boolean
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_rows integer;
  v_now timestamptz := clock_timestamp();
begin
  if p_project_id is null
     or length(btrim(p_project_id)) = 0
     or p_user_id is null
     or p_lease_token is null
     or jsonb_typeof(coalesce(p_error, '{}'::jsonb)) is distinct from 'object'
  then
    raise exception using
      errcode = '22023',
      message = 'invalid_stage_release';
  end if;

  update public.projects as p
     set generation_state = p.generation_state || jsonb_build_object(
           'last_stage_error', coalesce(p_error, '{}'::jsonb),
           'last_stage_error_at', to_jsonb(v_now)
         ),
         context = jsonb_set(
           p.context,
           '{generation_state}',
           p.generation_state || jsonb_build_object(
             'last_stage_error', coalesce(p_error, '{}'::jsonb),
             'last_stage_error_at', to_jsonb(v_now)
           ),
           true
         ),
         context_version = p.context_version + 1,
         stage_lease_token = null,
         stage_lease_name = null,
         stage_lease_expires_at = null,
         updated_at = v_now
   where p.id = p_project_id
     and p.user_id = p_user_id
     and p.stage_lease_token = p_lease_token;

  get diagnostics v_rows = row_count;
  return v_rows = 1;
end;
$$;

revoke all on function public.save_project_context(
  text, uuid, bigint, text, text, text, jsonb, jsonb
) from public, anon, authenticated;
grant execute on function public.save_project_context(
  text, uuid, bigint, text, text, text, jsonb, jsonb
) to service_role;

revoke all on function public.claim_generation_stage(
  text, uuid, bigint, text, integer
) from public, anon, authenticated;
grant execute on function public.claim_generation_stage(
  text, uuid, bigint, text, integer
) to service_role;

revoke all on function public.claim_generation_stage_idempotent(
  text, uuid, bigint, text, uuid, integer
) from public, anon, authenticated;
grant execute on function public.claim_generation_stage_idempotent(
  text, uuid, bigint, text, uuid, integer
) to service_role;

revoke all on function public.commit_generation_stage(
  text, uuid, bigint, text, uuid, text, text, text, jsonb, jsonb, uuid, jsonb
) from public, anon, authenticated;
grant execute on function public.commit_generation_stage(
  text, uuid, bigint, text, uuid, text, text, text, jsonb, jsonb, uuid, jsonb
) to service_role;

revoke all on function public.release_generation_stage(
  text, uuid, uuid, jsonb
) from public, anon, authenticated;
grant execute on function public.release_generation_stage(
  text, uuid, uuid, jsonb
) to service_role;

drop function if exists public.claim_user_quota(uuid, text, integer);
drop function if exists public.claim_user_quota(uuid, text, integer, uuid);

create or replace function public.claim_user_quota(
  p_user_id uuid,
  p_kind text,
  p_limit integer,
  p_idempotency_key uuid,
  p_fingerprint text
)
returns integer
language plpgsql
security definer
set search_path = ''
as $$
declare
  v_date date := (timezone('utc', clock_timestamp()))::date;
  v_count integer;
  v_new_claim integer;
  v_existing_fingerprint text;
begin
  if p_user_id is null
     or p_kind not in ('project_create', 'discovery', 'generation_stage')
     or p_limit is null
     or p_limit < 1
     or p_limit > 10000
     or p_idempotency_key is null
     or p_fingerprint is null
     or p_fingerprint !~ '^[0-9a-f]{64}$'
  then
    raise exception using
      errcode = '22023',
      message = 'invalid_quota_claim';
  end if;

  insert into public.usage_daily (user_id, usage_date)
  values (p_user_id, v_date)
  on conflict (user_id, usage_date) do nothing;

  insert into public.usage_claims (
    user_id, usage_date, kind, idempotency_key, fingerprint
  )
  values (
    p_user_id, v_date, p_kind, p_idempotency_key, p_fingerprint
  )
  on conflict (user_id, usage_date, kind, idempotency_key) do nothing;

  get diagnostics v_new_claim = row_count;
  if v_new_claim = 0 then
    select c.fingerprint
      into v_existing_fingerprint
      from public.usage_claims as c
      where c.user_id = p_user_id
        and c.usage_date = v_date
        and c.kind = p_kind
        and c.idempotency_key = p_idempotency_key;

    if not found or v_existing_fingerprint is distinct from p_fingerprint then
      raise exception using
        errcode = 'P0001',
        message = 'quota_idempotency_conflict';
    end if;

    select case p_kind
      when 'project_create' then u.project_creates
      when 'discovery' then u.discovery_calls
      when 'generation_stage' then u.generation_stages
    end
      into v_count
      from public.usage_daily as u
      where u.user_id = p_user_id
        and u.usage_date = v_date;
    return coalesce(v_count, 0);
  end if;

  update public.usage_daily as u
     set project_creates = u.project_creates
                           + case when p_kind = 'project_create' then 1 else 0 end,
         discovery_calls = u.discovery_calls
                           + case when p_kind = 'discovery' then 1 else 0 end,
         generation_stages = u.generation_stages
                           + case when p_kind = 'generation_stage' then 1 else 0 end,
         total_calls = u.total_calls + 1
   where u.user_id = p_user_id
     and u.usage_date = v_date
     and case p_kind
       when 'project_create' then u.project_creates
       when 'discovery' then u.discovery_calls
       when 'generation_stage' then u.generation_stages
     end < p_limit
  returning case p_kind
    when 'project_create' then u.project_creates
    when 'discovery' then u.discovery_calls
    when 'generation_stage' then u.generation_stages
  end into v_count;

  if not found then
    raise exception using
      errcode = 'P0001',
      message = 'daily_quota_exceeded';
  end if;
  return v_count;
end;
$$;

revoke all on function public.claim_user_quota(uuid, text, integer, uuid, text)
  from public, anon, authenticated;
grant execute on function public.claim_user_quota(uuid, text, integer, uuid, text)
  to service_role;

-- ---------------------------------------------------------------------------
-- Ownership helper and row-level security

create or replace function public.is_project_owner(p_project_id text)
returns boolean
language sql
stable
security definer
set search_path = ''
as $$
  select exists (
    select 1
    from public.projects as p
    where p.id = p_project_id
      and p.user_id = (select auth.uid())
  );
$$;

revoke all on function public.is_project_owner(text) from public;
grant execute on function public.is_project_owner(text) to authenticated, service_role;

alter table public.profiles enable row level security;
alter table public.projects enable row level security;
alter table public.conversations enable row level security;
alter table public.messages enable row level security;
alter table public.workflow_runs enable row level security;
alter table public.agent_runs enable row level security;
alter table public.artifacts enable row level security;
alter table public.api_calls enable row level security;
alter table public.usage_daily enable row level security;
alter table public.usage_claims enable row level security;

drop policy if exists profiles_select_own on public.profiles;
create policy profiles_select_own
on public.profiles for select
to authenticated
using (id = (select auth.uid()));

drop policy if exists profiles_insert_own on public.profiles;

drop policy if exists profiles_update_own on public.profiles;
create policy profiles_update_own
on public.profiles for update
to authenticated
using (id = (select auth.uid()))
with check (id = (select auth.uid()));

drop policy if exists projects_select_own on public.projects;
create policy projects_select_own
on public.projects for select
to authenticated
using (user_id = (select auth.uid()));

drop policy if exists projects_insert_own on public.projects;

drop policy if exists projects_update_own on public.projects;

drop policy if exists projects_delete_own on public.projects;

drop policy if exists conversations_select_own on public.conversations;
create policy conversations_select_own
on public.conversations for select
to authenticated
using (public.is_project_owner(project_id));

drop policy if exists conversations_insert_nondefault on public.conversations;

drop policy if exists conversations_update_own on public.conversations;

drop policy if exists conversations_delete_nondefault on public.conversations;

drop policy if exists messages_select_own_project on public.messages;
create policy messages_select_own_project
on public.messages for select
to authenticated
using (
  public.is_project_owner(project_id)
);

drop policy if exists messages_insert_user_only on public.messages;

drop policy if exists workflow_runs_select_own_project on public.workflow_runs;
create policy workflow_runs_select_own_project
on public.workflow_runs for select
to authenticated
using (public.is_project_owner(project_id));

drop policy if exists agent_runs_select_own_project on public.agent_runs;
create policy agent_runs_select_own_project
on public.agent_runs for select
to authenticated
using (public.is_project_owner(project_id));

drop policy if exists artifacts_select_own_project on public.artifacts;
create policy artifacts_select_own_project
on public.artifacts for select
to authenticated
using (public.is_project_owner(project_id));

drop policy if exists api_calls_select_own_project on public.api_calls;
create policy api_calls_select_own_project
on public.api_calls for select
to authenticated
using (public.is_project_owner(project_id));

drop policy if exists usage_daily_select_own on public.usage_daily;
create policy usage_daily_select_own
on public.usage_daily for select
to authenticated
using (user_id = (select auth.uid()));

-- Explicit privileges complement RLS. Authenticated browser sessions are
-- read-only for every application-data table; all mutations go through the
-- authenticated backend, which uses service_role after verifying the user.
revoke all on table public.profiles from anon, authenticated;
revoke all on table public.projects from anon, authenticated;
revoke all on table public.conversations from anon, authenticated;
revoke all on table public.messages from anon, authenticated;
revoke all on table public.workflow_runs from anon, authenticated;
revoke all on table public.agent_runs from anon, authenticated;
revoke all on table public.artifacts from anon, authenticated;
revoke all on table public.api_calls from anon, authenticated;
revoke all on table public.usage_daily from anon, authenticated;
revoke all on table public.usage_claims from anon, authenticated;

grant select on table public.profiles to authenticated;
grant update (display_name, avatar_url, preferences) on table public.profiles to authenticated;

grant select on table public.projects to authenticated;
grant select on table public.conversations to authenticated;
grant select on table public.messages to authenticated;
grant select on table public.workflow_runs to authenticated;
grant select on table public.agent_runs to authenticated;
grant select on table public.artifacts to authenticated;
grant select on table public.api_calls to authenticated;
grant select on table public.usage_daily to authenticated;

grant all privileges on table public.profiles to service_role;
grant all privileges on table public.projects to service_role;
grant all privileges on table public.conversations to service_role;
grant all privileges on table public.messages to service_role;
grant all privileges on table public.workflow_runs to service_role;
grant all privileges on table public.agent_runs to service_role;
grant all privileges on table public.artifacts to service_role;
grant all privileges on table public.api_calls to service_role;
grant all privileges on table public.usage_daily to service_role;
grant all privileges on table public.usage_claims to service_role;

commit;
