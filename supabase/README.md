# Supabase persistence and legacy migration

This directory contains the production database migration for B2D and the
instructions for importing the two existing SQLite/data snapshots. The import
is deliberately **dry-run by default**. Nothing is sent to Supabase unless the
command includes `--apply`.

## What the migration creates

The migration at
[`migrations/202608220001_initial_b2d.sql`](migrations/202608220001_initial_b2d.sql)
creates:

- `profiles`, linked one-to-one to `auth.users`, plus a signup trigger and a
  backfill for users that already exist;
- owner-scoped `projects`, with canonical context and generation state stored
  as JSONB, an optimistic `context_version`, and an expiring named-stage lease;
- `conversations` and ordered `messages`, including one automatically-created
  default conversation for every project;
- `workflow_runs` and `agent_runs` for the seven-agent execution history;
- `artifacts` for both rendered text and structured agent output;
- `api_calls` for sanitized provider telemetry only;
- `usage_daily` plus private, request-fingerprint-bound `usage_claims` receipts
  and an atomic service-role RPC that enforce conservative, retry-safe
  per-account daily project, discovery, and generation-stage allowances;
- foreign-key and query indexes, `updated_at` triggers, grants, and row-level
  security policies.

Supabase Auth owns passwords, access/refresh tokens, MFA, and sessions. None of
those values belong in `profiles` or any application table.

Authenticated browser users are read-only for `projects`, `conversations`,
`messages`, `workflow_runs`, `agent_runs`, `artifacts`, `api_calls`, and
`usage_daily`. `usage_claims` is backend-only. They may
select their own `profiles` row and update only `display_name`, `avatar_url`, and
`preferences`. The frontend sends every application mutation, including user
messages, through the authenticated backend API. The backend verifies the user
and performs trusted writes with `service_role`. Never expose the service-role
key or any Gemini key in browser JavaScript.

## 1. Create the Supabase project and owner account

1. Create the Supabase project.
2. In Supabase Auth, create or invite the account that should own all imported
   legacy projects.
3. Copy that Auth user's UUID. It becomes `MIGRATION_OWNER_ID`.

Do not insert rows directly into `auth.users` with SQL. Let Supabase Auth create
the account so its identity and login state remain valid.

## 2. Apply the SQL migration

Apply the entire SQL file before running the importer.

The simplest route is the Supabase dashboard's SQL editor:

1. Open **SQL Editor** in the target project.
2. Paste the complete contents of
   `supabase/migrations/202608220001_initial_b2d.sql`.
3. Run it once and confirm that the transaction commits.

For a repository already linked with the Supabase CLI, the same migration can
be applied through the normal migration workflow:

```powershell
supabase db push
```

The migration uses `IF NOT EXISTS`, `CREATE OR REPLACE`, and drop/recreate for
policies and triggers where practical, so re-running the exact file is safe for
the schema it creates. It is still intended to be tracked and applied as a
single migration; `CREATE TABLE IF NOT EXISTS` cannot repair an unrelated table
that happens to have the same name but a different shape.

After the migration, confirm the chosen Auth user has a row in `profiles`. The
signup trigger creates one for new users, and the migration backfills existing
Auth users.

### Backend project-write contract

The migration exposes five project-checkpoint RPCs (including the idempotent
serverless claim variant). Their function
signatures are:

```sql
save_project_context(text, uuid, bigint, text, text, text, jsonb, jsonb)
  returns bigint

claim_generation_stage(text, uuid, bigint, text, integer default 270)
  returns table (lease_token uuid, context_version bigint)

claim_generation_stage_idempotent(
  text, uuid, bigint, text, uuid, integer default 240
)
  returns table (lease_token uuid, context_version bigint)

commit_generation_stage(
  text, uuid, bigint, text, uuid, text, text, text, jsonb, jsonb,
  uuid default null, jsonb default '{}'::jsonb
)
  returns bigint

release_generation_stage(text, uuid, uuid, jsonb default '{}'::jsonb)
  returns boolean
```

`PUBLIC`, `anon`, and `authenticated` have no execute privilege on these
functions; only `service_role` does. The backend must still authenticate the
requesting user and pass that exact user's UUID as `p_user_id`.

`save_project_context` updates only the exact `(project_id, user_id,
expected_version)` row, rejects an unexpired stage lease, and returns the
incremented version. A generation worker first calls
`claim_generation_stage`; the claim succeeds only while project status is
`generating`, `generation_state.next_stage` exactly equals the requested stage,
and the previous lease is absent or expired. The worker must pass the returned
version and token to `commit_generation_stage`. Commit checkpoints the project,
clears the lease, advances the version, and updates the optional workflow row
in the same database transaction. Terminal statuses set `completed_at`; all
workflow starts use `started_at = coalesce(started_at, now())`.

The serverless API uses `claim_generation_stage_idempotent` and supplies its
own UUID lease token. If the database committed but the HTTP response was lost,
replaying that same token once returns the already-held claim instead of
running the Gemini stage twice. The five-argument claim remains available for
other trusted workers.

The separate service-role-only function
`claim_user_quota(uuid, text, integer, uuid, text) returns integer` atomically
increments one UTC-day counter for `project_create`, `discovery`, or
`generation_stage`. The UUID is a backend-derived, user/kind/project-scoped
form of the browser operation's `Idempotency-Key`; the final text argument is
the canonical 64-character SHA-256 operation fingerprint. Replaying an exact
key/fingerprint pair returns the existing claim without incrementing the
counter. Reusing that scoped key with different request semantics raises
`quota_idempotency_conflict` before Gemini runs. Generation fingerprints include
the claimed stage name so retries cannot alias a different workflow stage.

The function raises `daily_quota_exceeded` before a Gemini call once the
configured limit is reached. Browser roles can read only their own counter row
and cannot reset or increment it directly. Runtime defaults are controlled by
`DAILY_PROJECT_LIMIT`, `DAILY_DISCOVERY_LIMIT`, and
`DAILY_GENERATION_STAGE_LIMIT`.

Version/owner/lease/stage misses raise SQLSTATE `40001` with the stable message
`project_write_conflict`, `generation_stage_conflict`, or
`workflow_run_conflict`. Treat those as reload/reconcile responses, not as an
instruction to retry stale output blindly. A null `next_stage` never falls back
to `requirements`: it cannot be claimed and means the runtime should return a
complete/no-op response before marking a workflow as running.

## 3. Set migration-only environment variables

Set these values only in the terminal session used for migration:

```powershell
$env:MIGRATION_OWNER_ID = "<auth-user-uuid>"
$env:SUPABASE_URL = "https://<project-ref>.supabase.co"
$env:SUPABASE_SERVICE_ROLE_KEY = "<server-only-service-role-key>"
```

The importer requires all three names even in dry-run mode so the exact target
ownership can be validated while building deterministic payloads. It never
prints their values.

The script uses `httpx`, which is already part of the backend requirements. If
needed, install the project dependencies first:

```powershell
python -m pip install -r requirements.txt
```

## 4. Run the safe dry run

From `B2D V1.1`:

```powershell
python .\scripts\migrate_legacy_to_supabase.py --dry-run
```

`--dry-run` is optional because it is the default. The command reads, but never
writes to:

- `B2D V1.1/data/b2d.db`, `runs/`, and `artifacts/`;
- `../New folder2/data/b2d.db`, `runs/`, and `artifacts/`.

Its output contains counts and SHA-256 checksums only. It does not print
business ideas, chats, project IDs, filesystem paths, Supabase values, Gemini
values, provider responses, or secret-bearing error bodies.

Review that the source and target counts are plausible. Running the same dry
run against unchanged data should produce the same source and target checksums.
The placeholder conversation UUIDs used only during dry-run may differ from the
real default-conversation UUIDs already present in a target database, so the
message payload checksum after an applied migration is not intended as a
cross-environment database checksum.

The dry run also performs two local QA suites before any possible network
write:

- `qa.canonical_fidelity_checks` proves that legitimate canonical fields named
  `authorization`, `token`, and `password` remain byte-for-byte values in
  project context, message text, artifact text, and artifact structured data,
  while the same keys are redacted in metadata;
- `qa.target_structural_checks` verifies IDs, ownership, foreign references,
  project/context identity, canonical generation-state consistency, roles and
  turn indexes, artifact byte counts/hashes, and metadata sanitization.

For the repository snapshots reviewed on 2026-08-22, the expected target row
counts are `projects=16`, `workflow_runs=7`, `agent_runs=15`, `artifacts=38`,
`api_calls=15`, and `messages=42`. The same snapshot produces
`qa.canonical_fidelity_checks=11` and `qa.target_structural_checks=1035`. Treat
a change as a prompt to review the source snapshots and checksums; do not force
the old count.

Useful optional limits:

```powershell
python .\scripts\migrate_legacy_to_supabase.py --dry-run `
  --batch-size 100 `
  --max-artifact-bytes 2097152 `
  --timeout-seconds 30
```

The importer rejects non-UTF-8 or oversized legacy artifacts instead of
silently losing them. Raise `--max-artifact-bytes` deliberately if a reviewed
text artifact is larger than the default 2 MiB.

## 5. Apply the import

Only this explicit flag enables network writes:

```powershell
python .\scripts\migrate_legacy_to_supabase.py --apply
```

The importer:

1. opens both SQLite databases in read-only URI mode;
2. merges projects by their existing text ID and keeps the context with the
   newest `updated_at` value, preferring the current backend snapshot on an
   exact timestamp tie;
3. creates one project row and uses its default conversation;
4. turns each latest ProjectContext transcript into ordered messages;
5. pairs JSONL `started` and terminal records by project, agent, and
   `started_at`, then imports workflow, agent-run, and API telemetry rows;
6. imports each structured agent output as `<output-name>.json` in `artifacts`;
7. imports rendered artifact files as UTF-8 text with MIME type, byte size, and
   SHA-256 hash;
8. preserves canonical `projects.context`, message content, artifact text, and
   artifact `structured_data` verbatim; key-based redaction is applied only to
   telemetry, API metadata, and run metadata;
9. upserts deterministic IDs in dependency order.

PostgREST requests are not one cross-table transaction. Deterministic IDs and
upserts make the command safe to rerun after a partial network failure.

As an ownership safeguard, the importer aborts if a matching project ID already
belongs to another Supabase user. Reassignment is possible only with the
explicit flag below and should be used after reviewing the target project:

```powershell
python .\scripts\migrate_legacy_to_supabase.py --apply --allow-owner-reassignment
```

The importer also refuses to overwrite a matching project whose
`context_version` is above zero or which carries any stage lease, including an
expired lease. This prevents an import or retry from replacing live workflow
state. A normal partial-import retry remains idempotent because freshly
imported project rows stay at version zero and carry no lease until the backend
starts mutating them.

## 6. Clear migration secrets

After a successful import, remove the migration-only variables from the current
PowerShell process:

```powershell
Remove-Item Env:MIGRATION_OWNER_ID -ErrorAction SilentlyContinue
Remove-Item Env:SUPABASE_SERVICE_ROLE_KEY -ErrorAction SilentlyContinue
Remove-Item Env:SUPABASE_URL -ErrorAction SilentlyContinue
```

Do not commit these values to `.env`, the repository, browser code, build logs,
or screenshots.

## Runtime environment names

The eventual backend integration should use server-only variables such as:

- `SUPABASE_URL`
- `SUPABASE_SERVICE_ROLE_KEY`
- the existing seven `GEMINI_*_API_KEY` variables and Gemini/LLM settings
- `FRONTEND_ORIGIN`

The browser needs only the public Supabase URL and publishable/anon key. For a
bundled frontend these would commonly be exposed at build time as
`VITE_SUPABASE_URL` and `VITE_SUPABASE_ANON_KEY`. They are public identifiers,
but all access remains constrained by the RLS policies. The service-role and
Gemini keys must remain server-only.

## Data-retention notes

- Keep canonical project context, messages, agent outputs, and artifacts until
  the owner deletes the project; foreign keys cascade the database rows.
- `api_calls` intentionally stores sanitized metadata and telemetry, not raw
  authorization headers or provider credentials. A scheduled retention job can
  prune old API-call rows without deleting canonical agent output.
- Large or binary artifacts should later move to a private Supabase Storage
  bucket. The present importer targets the existing small UTF-8 text artifacts
  and stores them in the backend-compatible `artifacts.content_text` column.
