# B2D — Business Idea to Engineering Blueprint

> An autonomous, multi-agent AI system that turns a vague business idea into a
> complete, validated software engineering blueprint.

**Built for the DevOps Hackathon.** You type one sentence — "I want to build a
platform where users can book football fields" — and a team of AI agents takes
over: it interviews you, drafts requirements, designs the architecture, the
database, the API, and the full DevOps stack (Dockerfile, `docker-compose.yml`,
GitHub Actions CI/CD), then cross-reviews everything for consistency before
shipping a set of human-readable artifacts.

---

## Table of Contents

1. [What It Is](#what-it-is)
2. [High-Level Architecture](#high-level-architecture)
3. [The Full Workflow](#the-full-workflow)
4. [Project Lifecycle](#project-lifecycle)
5. [The Agent Team](#the-agent-team)
6. [The LLM Layer](#the-llm-layer)
7. [The Orchestrator](#the-orchestrator)
8. [Data Model (Pydantic Schemas)](#data-model-pydantic-schemas)
9. [Prompts](#prompts)
10. [Generated Artifacts](#generated-artifacts)
11. [REST API](#rest-api)
12. [Persistence & Run Tracking](#persistence--run-tracking)
13. [Events & Live Streaming](#events--live-streaming)
14. [Project Structure](#project-structure)
15. [Installation & Setup](#installation--setup)
16. [Configuration](#configuration)
17. [Running the System](#running-the-system)
18. [Running Tests](#running-tests)
19. [Extending the System](#extending-the-system)
20. [Security Notes](#security-notes)

---

## What It Is

`B2D` (Backend-to-Deployment / Business-to-DevOps) is a Python package that
implements an **agentic AI core**. Instead of a single monolithic LLM call, it
uses a **team of specialized agents**, each with a single responsibility, wired
together by an **orchestrator** that enforces an order, retries failures
boundedly, and runs a **single, evidence-based consistency review** before
delivering the blueprint.

Key properties:

- **Human-in-the-loop discovery** — the system asks targeted questions until it
  genuinely understands the project before generating anything.
- **Structured, validated outputs** — every agent must return JSON matching a
  strict Pydantic schema; malformed responses are automatically repaired with a
  bounded number of retries.
- **Dependency-aware parallel engineering** — requirements and architecture
  establish the shared design, then Database and API run concurrently before
  DevOps consumes both outputs.
- **Bounded, convergent review** — the Reviewer runs at most **once**. If it
  finds blocking inconsistencies, only the flagged artifacts are **revised** (a
  targeted edit of the existing artifact, max one revision each) and the
  workflow completes — it never re-reviews, so it can never loop forever.
- **Provider-agnostic LLM layer** — the entire system depends on a small
  `LLMProvider` interface. It ships with Cursor, Kimi, OpenRouter, Gemini, and a
  Fake provider for tests. Gemini uses one distinct key per agent.
- **Observable** — every agent run is recorded to JSONL (with per-call
  telemetry: call id, model, TTFT, duration, tokens) and live progress is
  streamed over Server-Sent Events (SSE).

---

## High-Level Architecture

```
                         ┌──────────────────────────────────────────┐
                         │              Frontend / Client           │
                         │  (CLI, scripted demo, or your own UI)    │
                         └──────────────────┬───────────────────────┘
                                            │ REST + SSE
                                   ┌────────▼────────┐
                                   │  FastAPI layer   │  agentic_core/api/
                                   │  (thin adapter)  │
                                   └────────┬────────┘
                                            │
                                   ┌────────▼───────────────┐
                                   │      Orchestrator      │  agentic_core/orchestrator/
                                   │ discovery · order ·    │
                                   │ review · regeneration  │
                                   └────────┬───────────────┘
                                            │ emits events
                                   ┌────────▼────────┐       ┌──────────────────┐
                                   │    EventBus     │──────▶│   SSE streams    │
                                   │  + per-project  │       │  to subscribers   │
                                   │   buffer (500)  │       └──────────────────┘
                                   └─────────────────┘
                                            │
                    ┌───────────────────────┼────────────────────────┐
                    ▼                       ▼                        ▼
          ┌─────────────────┐    ┌──────────────────┐    ┌───────────────────┐
          │   Agent team    │    │   LLMService     │    │  Persistence      │
          │ discovery       │───▶│  structured JSON │    │  ProjectStore     │
          │ requirements    │    │  + repair retry  │    │  ExecutionTracker │
          │ architecture    │    └────────┬─────────┘    │  ArtifactStore    │
          │ database        │             │             └───────────────────┘
          │ api             │     ┌───────▼────────┐
          │ devops          │     │  LLMProvider   │
          │ reviewer        │     │  (interface)   │
          └─────────────────┘     ├────────────────┤
                                  │ CursorCloud    │  real API
                                  │ Fake           │  tests/offline
                                  └────────────────┘
```

**Separation of concerns.** The frontend only talks to the FastAPI adapter. The
adapter talks to the orchestrator. The orchestrator talks to agents. Agents only
talk to `LLMService`. The `LLMService` is the only component that talks to an
LLM provider. Agents never contain workflow logic, never touch a provider SDK,
and never know about the UI.

---

## The Full Workflow

The entire journey of a project can be broken into five phases.

### Phase 1 — Discovery (conversational requirement elicitation)

The **Discovery Agent** is the human-facing intelligence layer.

1. You provide a vague business idea (e.g. *"I want to build a platform where
   users can book football fields"*).
2. The agent analyzes the idea, the current understanding, and the full
   conversation transcript.
3. It returns a structured `DiscoveryOutput`:
   - `status`: `needs_clarification` or `ready`
   - `confidence`: 0.0–1.0 (must be high, ≥ 0.9, to reach `ready`)
   - `summary`: a 2–3 sentence recap of its understanding
   - `known_information`: its best understanding of every canonical field
   - `missing_information`: which fields are still missing and how important
     (`critical` / `optional` / `not_applicable`)
   - `questions`: 1–4 focused questions (at most 4), each with multiple-choice
     `options`; asks none if the answers so far are enough (never re-asks what
     it already knows)
4. Your answers are appended to the transcript and the loop repeats until the
   agent decides it has enough critical information. All answers for a turn are
   sent to the agent in a single run so discovery converges in 1–3 turns.

Rules the agent follows (from its system prompt): never invent requirements,
record assumptions explicitly, prefer open questions over yes/no, let the
latest answer win on contradiction, and classify irrelevant fields as
`not_applicable` instead of asking about them.

When `status == "ready"`, the project transitions to
`ready_for_confirmation`.

### Phase 2 — Confirmation gate

The system prints "YOUR PROJECT UNDERSTANDING" (problem, target users, roles,
goals, features, constraints, integrations, tech preferences) and asks you to
confirm. `Orchestrator.confirm()` is a strict state gate — it raises
`OrchestrationError` if the project is not in `ready_for_confirmation`. On
confirmation the status becomes `confirmed`, which is the only status from
which generation is allowed.

### Phase 3 — Autonomous engineering (dependency-ordered)

Once confirmed, the orchestrator runs dependency-safe stages:

```
requirements → architecture → (database || api) → devops
```

Each agent receives only the inputs it needs:

| Agent          | Inputs                                                       |
|----------------|--------------------------------------------------------------|
| requirements   | project context                                              |
| architecture   | project context + requirements                               |
| database       | project context + requirements + architecture                |
| api            | project context + requirements + architecture                |
| devops         | project context + requirements + architecture + database + api |

An agent that fails due to a provider/transport error (network, poll timeout,
auth) is run once more (`_run_with_retry`). A structured-output failure already
consumed its internal repair retries, so it is **not** re-run — a full second
run would only double the token cost. If an agent still fails, the workflow
stops and the project is marked `needs_attention`.

### Phase 4 — Review & bounded regeneration

After the five engineering agents succeed, the **Review Agent** cross-validates
every artifact for internal consistency (see
[The Agent Team](#the-agent-team) for the mandatory checks). The reviewer
receives only compact artifact digests — one copy of each — so its input stays
small and stable (no discovery transcript, no previous review output).

- If `status == "approved"`, the workflow completes as `approved` and artifacts
  are rendered.
- If `status == "needs_revision"`, the reviewer returns `issues[]` with
  `severity` of `blocking` / `warning` / `suggestion`. **Only blocking issues
  trigger regeneration**, and each must cite the exact source and conflicting
  decision so the fix can be targeted.
- Blocking targets are expanded through the `DEPENDENTS` map so anything built
  **on top of** a regenerated artifact is also regenerated:

  ```
  requirements → requirements, architecture, database, api, devops
  architecture  → architecture, database, api, devops
  database      → database, devops
  api           → api, devops
  devops        → devops
  ```

- Regeneration is a **revision**, not a redo: each affected agent receives its
  existing artifact plus the exact reviewer issues and is told to preserve every
  valid decision.
- Bounds (config): the reviewer runs at most `max_review_rounds` (default `1`)
  and each artifact is revised at most `max_artifact_revisions` (default `1`).
  After the single regeneration pass the workflow completes with `revised`
  (all flagged artifacts regenerated) or `needs_attention` (a revision failed,
  hit its cap, or produced no change) — it **never re-reviews**.
- A failed regeneration never overwrites the previous successful artifact, and
  transient provider failures get at most `max_llm_retries` (default `1`).

### Phase 5 — Artifacts

`render_all()` turns the structured agent outputs into human/ops-readable
files saved under `data/artifacts/<project_id>/`:

| File                 | Source                              |
|----------------------|-------------------------------------|
| `overview.md`        | project context                     |
| `requirements.md`    | requirements agent                  |
| `architecture.md`    | architecture agent                  |
| `architecture.mmd`   | Mermaid flow diagram                |
| `database.md`        | database agent (entities, ERD text) |
| `database.sql`       | executable SQL schema               |
| `erd.mmd`            | Mermaid ER diagram                  |
| `api.md`             | API design (endpoints, auth, …)     |
| `openapi.yaml`       | OpenAPI 3.0 spec                    |
| `devops.md`          | deployment strategy, health, CI/CD  |
| `Dockerfile`         | complete backend Dockerfile         |
| `docker-compose.yml` | local stack (backend + DB + services) |
| `github-actions.yml` | CI/CD workflow                      |

---

## Project Lifecycle

A project's `status` field moves through a strict state machine:

```
discovery ─▶ ready_for_confirmation ─▶ confirmed ─▶ generating ─▶ approved
     ▲                                        │                     │
     │                                        │              ┌──────┴──────┐
     │                                        ├──▶ revised ◀──┤    review   │
     │                                        │              │ (1 round)   │
     └────── (stay in discovery until ready)  │              └──────┬──────┘
                                             │              needs_attention
                                             └────▶ needs_attention ◀────┘
                                                        (failure or revision cap)
```

| Status                   | Meaning                                                        |
|--------------------------|----------------------------------------------------------------|
| `discovery`              | Agent still asking clarifying questions                        |
| `ready_for_confirmation` | Discovery complete; waiting for the user to confirm            |
| `confirmed`              | User confirmed; generation allowed                             |
| `generating`             | Engineering agents are running                                 |
| `approved`               | Blueprint passed the review                                    |
| `revised`                | Blocking issues were fixed by one targeted regeneration pass   |
| `needs_attention`        | An agent failed repeatedly, a revision failed, or the revision cap was hit |

Each project is stored as a row in a SQLite database (`data/b2d.db`). Projects
saved by older versions as `data/projects/*.json` files are imported
automatically on startup.

---

## The Agent Team

All agents extend `BaseAgent` (`agentic_core/agents/base.py`), which provides:

- a tracked `run(context, revision=None)` lifecycle that measures `duration_ms`
  and records per-call LLM telemetry (call id, model, TTFT, tokens),
- structured-output execution against a per-agent `output_schema`,
- per-run `_stats["repair_count"]` (number of JSON repair retries),
- optional `ExecutionTracker` recording of every run,
- targeted-revision support: when the orchestrator passes a
  `RevisionInstruction` (existing artifact + reviewer issues), the agent revises
  only the flagged decisions instead of regenerating from scratch.

### Discovery Agent (`agents/discovery.py`)

The only human-facing agent. Runs an adaptive conversation, updates the
project context via `apply_known_information`, and decides when to stop asking.
Helper functions in the module:

- `known_info_snapshot(context)` — canonical current understanding.
- `apply_known_information(context, known)` — idempotently overwrites context
  fields (list vs. string handling, `None` skip).
- `discovery_agent_message(output)` — the human-readable agent turn appended to
  the transcript.
- `format_transcript(context)` — last 10 conversation turns, formatted.

Every question carries multiple-choice `options` (3-6 concrete choices). The
user can answer by picking option numbers (e.g. `1,3`) or by typing their own
text — the CLI's `parse_user_answer` handles both. To keep discovery fast, all
answers in a turn are sent to the agent in **one** run, and the agent only
reports `known_information` fields that changed or were newly inferred.

### Requirements Engineer (`agents/requirements.py`)

Produces `functional_requirements`, `non_functional_requirements`,
`user_stories`, `acceptance_criteria`, `constraints`, and `assumptions`. Every
functional requirement must be traceable to the context; never invents
constraints.

### Architecture Agent (`agents/architecture.py`)

Designs `system_components` (name/type/description/technology), communication,
authentication, security, scalability, `technology_stack`, deployment
architecture, and a Mermaid `flowchart`. Must honor tech preferences and pick
exactly one primary database technology.

### Database Design Agent (`agents/database.py`)

Designs entities with typed fields (PK/FK/nullable/unique/indexed), relations,
indexes, and constraints. The executable `sql_schema` and Mermaid `erDiagram`
are **derived locally** from the entity/field metadata (see `render.py`), so the
agent never spends output tokens on them. The database technology **must** match
the architecture's database component.

### API Design Agent (`agents/api.py`)

Designs REST `endpoints` (method, path, summary, auth, request/response
schemas, pagination, filters), authentication, authorization (using the context
user roles), error handling, pagination/filtering strategy. The full OpenAPI 3.0
document is **derived locally** from the endpoints (see `render.py`), so the
agent never spends output tokens on it. No endpoint may reference a nonexistent
entity.

### DevOps Engineer Agent (`agents/devops.py`)

The star of a DevOps hackathon. Produces a `Dockerfile` (correct base image,
non-root user, healthcheck, minimal layers), `docker-compose.yml`, a CI/CD
pipeline description, a complete GitHub Actions workflow, env vars (placeholders
only — never real secrets), deployment strategy, health checks, logging,
monitoring, and secrets management. All technologies must match the
architecture. Artifacts are **for review only** and never executed.

### Review Agent (`agents/reviewer.py`)

Cross-validates everything from compact artifact digests. Mandatory consistency
checks:

1. Requirements ↔ Architecture
2. Architecture ↔ Database (technology must match — Postgres vs Mongo is a
   blocking conflict)
3. Architecture ↔ API
4. Database ↔ API (endpoints must map to real entities/fields)
5. Architecture ↔ DevOps (Dockerfile, compose, CI/CD must use the same stack)
6. Security consistency (coherent auth/authorization across all artifacts)
7. Technology consistency (no artifact may introduce a contradictory tech)

Every issue is structured: `artifact`, `severity` (`blocking` / `warning` /
`suggestion`), `problem`, `expected`, `actual`, `fix`, plus the evidence
(`source_artifact`, `source_decision`, `conflicting_artifact`,
`conflicting_decision`). **Only `blocking` issues trigger regeneration**;
warnings and suggestions never do, and the reviewer must cite concrete evidence
rather than "this could be improved". Responses are kept to 200–500 tokens. The
orchestrator derives the minimal `artifacts_to_regenerate` set from the blocking
issues and expands downstream dependents itself.

---

## The LLM Layer

### Provider abstraction (`llm/base.py`)

```python
class LLMProvider(ABC):
    async def generate(self, system_prompt: str, user_prompt: str, stats: dict | None = None) -> str: ...
```

This is the *only* interface the whole system depends on. Swap providers
without touching agent or orchestrator code. These implementations ship:

- **`FakeLLMProvider`** — in-memory, scripted responses or a callable handler.
  Used by the entire test suite and ideal for offline demos.
- **`CursorCloudProvider`** (`llm/cursor_provider.py`) — talks to Cursor's
  Cloud Agents API (`https://api.cursor.com/v1`). Creates a short-lived
  *no-repo* agent with the combined prompt, polls its run to completion
  (every `llm_poll_interval_s` seconds, up to `llm_poll_timeout_s`), returns
  the final assistant text, then archives the agent. No secrets are logged.
- **`KimiProvider`** and **`OpenRouterProvider`** — use OpenAI-compatible chat
  completion APIs.
- **`GeminiProvider`** (`llm/gemini_provider.py`) — uses Gemini's native
  `generateContent` API with structured JSON responses. Application wiring
  creates seven isolated instances, one dedicated credential per agent.

### `LLMService` (`llm/service.py`)

The single entry point agents call: `await llm_service.generate(system, user, schema, stats)`.

Responsibilities:

1. **Schema embedding** — appends the target Pydantic model's JSON Schema to
   the user prompt and demands "only a single valid JSON object".
2. **Parsing** — `extract_json_object` tolerates prose, fenced code blocks
   (` ```json `), and stray braces around the JSON.
3. **Validation** — parses with the Pydantic schema; a `ValidationError` or
   `StructuredOutputError` triggers a repair.
4. **Bounded repair** — re-invokes the provider with the previous bad response
   and the exact validation error, asking for a clean JSON object only.
   Retries are capped at `structured_output_max_retries` (default `1`), then
   the agent fails and the orchestrator marks the run failed.

Custom exceptions: `LLMProviderError` (network/auth), `LLMGenerationError`
(unusable output), `StructuredOutputError` (unparseable/invalid JSON).

---

## The Orchestrator

`agentic_core/orchestrator/orchestrator.py` owns the workflow and is the only
component that knows about it.

Public API:

- `Orchestrator.discovery_turn(context, user_message)` — one discovery step;
  raises `DiscoveryError` if the discovery agent fails.
- `Orchestrator.confirm(context)` — the confirmation gate.
- `Orchestrator.generate(context)` — runs the full engineering pipeline plus a
  single bounded review/regeneration pass; returns a dict of `AgentResult`s
  keyed by name, plus `call_counts` and `revisions` (per-agent LLM invocation
  counts and revision counters).

Internals:

- `ENGINEERING_ORDER` — the fixed agent order.
- `DEPENDENTS` — the downstream-dependent expansion map used by
  `_regeneration_targets`.
- `_run_with_retry(context, name, revision, ...)` — re-runs an agent at most
  `max_llm_retries` times, but only for provider/transport failures
  (structured-output failures already exhausted their internal repairs and are
  not re-run — cost saving). Regeneration passes a `RevisionInstruction` so the
  run is a targeted edit, never a from-scratch redo.
- `_run_reviewer(...)` — the single review round (one run, one bounded retry).
- `_blocking_targets(review)` — only blocking issues become regeneration targets.
- `_artifact_hash(...)` — deterministic artifact fingerprint; if a revision
  produces no meaningful change the issue is reported instead of retried.

### Event & tracking support (`orchestrator/events.py`, `orchestrator/tracker.py`)

- **`EventBus`** — an in-process pub/sub bus. Per-project ring buffer (500
  events) so late-connecting SSE consumers still see history; `stream()`
  yields buffered then live events with 15s heartbeats. Events carry an
  `invocation` number so consumers can tell a first run from a regeneration.
- **`ExecutionTracker`** — appends a `RunRecord` (project, agent, status,
  input snapshot, output, error, timestamps, duration, retry count, cost
  metrics, and per-call LLM telemetry: `call_id`, `model`, `ttft_s`,
  `input_tokens`/`output_tokens`) per run to `data/runs/<project_id>.jsonl`.
  No secrets are ever written.

---

## Data Model (Pydantic Schemas)

All schemas live in `agentic_core/schemas/`. They serve double duty: the
in-memory/on-disk project state and the JSON schemas enforced on every LLM
response.

### `ProjectContext` (`schemas/context.py`) — the central state

The single object threaded through every phase. Holds:

- **Identity**: `project_id`, `business_idea`.
- **Discovery fields** (all filled by the Discovery Agent):
  `problem`, `target_users`, `user_roles`, `business_goals`, `core_features`,
  `scope`, `constraints`, `assumptions`, `integrations`,
  `security_requirements`, `performance_requirements`,
  `deployment_requirements`, `technology_preferences`,
  `auth_requirement`, `authorization_requirement`, `payment_requirement`,
  `notification_requirement`.
- **Artifacts** (filled by each engineering agent):
  `requirements`, `architecture`, `database`, `api`, `devops`, plus `review`.
- **Lifecycle**: `status`, `transcript` (list of `DiscoveryTurn`s), `updated_at`.

Helpers: `add_turn(role, message)` and `touch()` keep `updated_at` current.

### Per-agent output schemas

| Schema                        | Key fields                                                              |
|-------------------------------|-------------------------------------------------------------------------|
| `DiscoveryOutput`             | `status`, `confidence`, `summary`, `known_information`, `missing_information`, `questions` |
| `RequirementsOutput`          | `functional_requirements`, `non_functional_requirements`, `user_stories`, `acceptance_criteria`, `constraints`, `assumptions` |
| `ArchitectureOutput`          | `system_components[]`, `communication`, `authentication`, `security`, `scalability`, `technology_stack`, `deployment_architecture`, `mermaid_diagram` |
| `DatabaseOutput`              | `database_technology`, `entities[]`, `relationships`, `indexes`, `constraints` — `sql_schema`/`erd_mermaid` are excluded from the LLM schema and derived locally |
| `APIOutput`                   | `endpoints[]`, `authentication`, `authorization`, `error_handling`, `pagination`, `filtering` — `openapi_spec` is excluded from the LLM schema and derived locally |
| `DevopsOutput`                | `dockerfile`, `docker_compose`, `ci_cd_pipeline`, `github_actions`, `environment_variables`, `deployment_strategy`, `health_checks`, `logging`, `monitoring`, `secrets_management` |
| `ReviewOutput`                | `status` (`approved`/`needs_revision`), `score`, `issues[]`, `artifacts_to_regenerate` |

Supporting models: `DiscoveryQuestion`, `MissingInfo`, `DiscoveryTurn`,
`SystemComponent` (typed: frontend/backend/service/database/external/
infrastructure), `DBEntity`/`DBField`, `APIEndpoint` (typed HTTP methods),
`ReviewIssue` (severity blocking/warning/suggestion + evidence fields).

---

## Prompts

`agentic_core/prompts/` holds a registry (`PROMPTS`) of `Prompt(name, system,
user_template)` per agent. User templates use `{__KEY__}` placeholders,
substituted at runtime by `build_user_prompt(name, **values)`.

The `build_user_prompt` machinery replaces `{__KEY__}` (uppercased) with the
provided values, e.g. the Requirements agent fills `{__PROJECT_CONTEXT__}`.
The `LLMService` then appends the JSON schema requirements.

Each system prompt follows the same structure for predictable behavior:
**Role · Objective · Input · Output · Consistency · Failure behaviour**.

---

## Generated Artifacts

`agentic_core/artifacts/render.py` converts validated structured outputs into
text. Notable functions:

- `render_overview(context)` → `overview.md`
- `render_requirements(...)` → `requirements.md`
- `render_architecture(...)` + `render_architecture_mmd(...)` → `architecture.md`, `architecture.mmd`
- `render_database_markdown(...)` + `render_database_sql(...)` + `render_erd(...)` → `database.md`, `database.sql`, `erd.mmd`
- `render_api_markdown(...)` + `render_openapi(...)` (YAML dump) → `api.md`, `openapi.yaml`
- `render_devops_markdown(...)` → `devops.md`
- `render_all(context)` → the complete `dict[filename, content]` of everything above.

`ArtifactStore` (`artifacts/store.py`) persists these on disk under
`data/artifacts/<project_id>/` with path-traversal protection (`_safe_name`).

---

## REST API

`agentic_core/api/app.py` is a thin FastAPI adapter (port **8000**). The
frontend never knows agent implementation details. CORS is open for all
origins (dev setting).

| Method | Endpoint                                           | Description                                             |
|--------|----------------------------------------------------|---------------------------------------------------------|
| POST   | `/api/projects`                                    | Create project + run first discovery turn               |
| POST   | `/api/projects/{id}/discovery/start`               | Start/restart discovery with a message                  |
| POST   | `/api/projects/{id}/discovery/message`             | Continue discovery with a user answer                   |
| GET    | `/api/projects/{id}/discovery/state`               | Current discovery state                                 |
| POST   | `/api/projects/{id}/discovery/confirm`             | Confirm understanding (409 unless ready_for_confirmation) |
| POST   | `/api/projects/{id}/generate`                      | Kick off engineering in the background (409 if already running) |
| GET    | `/api/projects/{id}/generation/status`             | **SSE** stream of agent events                          |
| GET    | `/api/projects/{id}`                               | Full project state                                      |
| GET    | `/api/projects/{id}/artifacts`                     | List rendered artifact filenames                        |
| GET    | `/api/projects/{id}/artifacts/{artifact_type}`     | Raw artifact content (plain text)                       |

Shared services are assembled once in `api/deps.py` (`AppServices`): settings,
event bus, tracker, Cursor provider, LLM service, orchestrator, project store,
artifact store, and a `generation_tasks` registry. `_run_generation` runs the
orchestrator in an `asyncio` task, saves the project, renders all artifacts into
the store, and emits a final `artifacts_ready` event.

### SSE event stream

The `/generation/status` endpoint streams `AgentEvent` JSON payloads with
event types: `workflow_started`, `agent_started`, `agent_completed`,
`agent_retrying`, `agent_failed`, `review_started`, `review_completed`,
`review_failed`, `workflow_completed`, `workflow_failed`, `artifacts_ready`
(plus 15s `heartbeat` keep-alives). The stream terminates with an SSE `done`
event after `workflow_completed` / `workflow_failed`.

---

## Persistence & Run Tracking

| Store               | Location                      | Format | Purpose                                    |
|---------------------|-------------------------------|--------|--------------------------------------------|
| `ProjectStore`      | `data/b2d.db`             | SQLite | Full project context + artifacts (JSON blobs) |
| `ExecutionTracker`  | `data/runs/<id>.jsonl`        | JSONL  | Append-only per-agent run history          |
| `ArtifactStore`     | `data/artifacts/<id>/`        | files  | Rendered markdown/SQL/YAML/Docker artifacts |

---

## Events & Live Streaming

`EventBus` (in `orchestrator/events.py`) is the progress backbone:

- Synchronous `subscribe(listener)` / `unsubscribe(listener)` for CLI/script
  progress printing.
- Per-project ring buffer (500 events) replayed to late-connecting consumers.
- `stream(project_id)` async generator used by the SSE endpoint, emitting a
  `heartbeat` every 15s of inactivity.

The CLI (`agentic_core/cli.py`) maps event types to symbols for a nice
terminal experience: `▶` workflow start, `→` agent start, `✓` completed,
`↻` retrying, `✗` failed, `◈` review, `⚠` review failed, `✔` completed.

---

## Project Structure

```
B2D/
├── agentic_core/                 # The Python package (the "core")
│   ├── __init__.py               # package metadata (v0.1.0)
│   ├── config.py                 # env-based Settings (pydantic-settings)
│   ├── cli.py                    # interactive CLI demo
│   ├── project_store.py          # JSON persistence for projects
│   ├── agents/                   # the agent team
│   │   ├── base.py               # BaseAgent + AgentResult + payload helper
│   │   ├── discovery.py
│   │   ├── requirements.py
│   │   ├── architecture.py
│   │   ├── database.py
│   │   ├── api.py
│   │   ├── devops.py
│   │   ├── reviewer.py
│   │   └── __init__.py           # build_agents() factory
│   ├── llm/                      # provider abstraction + service
│   │   ├── base.py               # LLMProvider, FakeLLMProvider, errors
│   │   ├── cursor_provider.py    # Cursor Cloud Agents provider
│   │   ├── gemini_provider.py    # Native Gemini generateContent provider
│   │   ├── kimi_provider.py      # Kimi chat-completions provider
│   │   ├── openrouter_provider.py
│   │   ├── service.py            # LLMService (schema + repair)
│   │   └── __init__.py
│   ├── orchestrator/             # workflow engine
│   │   ├── orchestrator.py       # Orchestrator, ENGINEERING_ORDER, DEPENDENTS
│   │   ├── events.py             # AgentEvent, EventBus
│   │   ├── tracker.py            # RunRecord, ExecutionTracker
│   │   └── __init__.py
│   ├── schemas/                  # Pydantic data models
│   │   ├── context.py            # ProjectContext, DiscoveryTurn, ProjectStatus
│   │   ├── discovery.py
│   │   ├── requirements.py
│   │   ├── architecture.py
│   │   ├── database.py
│   │   ├── api.py
│   │   ├── devops.py
│   │   ├── review.py
│   │   └── __init__.py
│   ├── prompts/                  # system prompts + user templates
│   │   ├── __init__.py           # PROMPTS registry, build_user_prompt()
│   │   └── discovery.py, requirements.py, architecture.py,
│   │       database.py, api.py, devops.py, reviewer.py
│   ├── artifacts/                # rendering + storage of final outputs
│   │   ├── render.py             # render_all() and friends
│   │   ├── store.py              # ArtifactStore
│   │   └── __init__.py
│   └── api/                      # FastAPI adapter
│       ├── app.py                # endpoints + SSE
│       ├── deps.py               # AppServices singleton
│       └── __init__.py
├── scripts/
│   ├── demo_football.py          # scripted end-to-end live demo
│   └── run_test.py               # headless E2E test + per-agent cost table
├── tests/                        # pytest suite (hermetic, fake LLM)
│   ├── conftest.py               # fixtures (settings, provider, orchestrator…)
│   ├── helpers.py                # valid sample outputs + build_handler()
│   ├── test_agents.py            # structured-output / failure handling
│   ├── test_discovery.py         # discovery conversation loop
│   ├── test_orchestrator.py      # order, retries, review loop, limits
│   └── test_e2e.py               # full workflow end-to-end
├── data/                         # runtime data (gitignored in a real repo)
│   ├── b2d.db                    # SQLite database of projects
│   ├── runs/                     # <project_id>.jsonl
│   └── artifacts/                # <project_id>/ rendered files
├── .env.example                  # documented environment template
├── .env                          # local secrets (NOT committed)
├── requirements.txt
├── pytest.ini                    # asyncio_mode = auto, testpaths = tests
└── README.md
```

---

## Installation & Setup

Requires **Python 3.11+** (the compiled artifacts in the tree are `cpython-311`).

```bash
# 1. Create and activate a virtual environment
python -m venv .venv
# Windows (PowerShell):
.venv\Scripts\Activate.ps1
# macOS / Linux:
source .venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Configure credentials
copy .env.example .env          # Windows
cp .env.example .env            # macOS / Linux
# ... then paste your Cursor API key into CURSOR_API_KEY
```

> Get a Cursor API key at https://cursor.com/dashboard/api
>
> For Gemini, set `LLM_PROVIDER=gemini` and fill all seven
> `GEMINI_*_API_KEY` entries in `.env` with different keys.

---

## Configuration

All settings are read from environment variables / `.env` (see
`agentic_core/config.py`). Secrets are only ever read from the environment and
are never logged.

| Variable                     | Default                         | Meaning                                      |
|------------------------------|---------------------------------|----------------------------------------------|
| `CURSOR_API_KEY`             | *(empty)*                       | Cursor Cloud Agents API key                  |
| `GEMINI_*_API_KEY`           | *(empty)*                       | Seven distinct keys, one for each agent      |
| `GEMINI_MODEL`               | `gemini-2.5-flash`              | Gemini model used by all agents              |
| `GEMINI_BASE_URL`            | Google Generative Language API  | Gemini REST base URL                          |
| `LLM_PROVIDER`               | `cursor`                        | `cursor`, `kimi`, `openrouter`, or `gemini`  |
| `LLM_API_KEY`                | *(empty)*                       | Optional override (preferred over cursor key)|
| `LLM_MODEL`                  | `default`                       | Model id override (`default` = provider's)   |
| `LLM_BASE_URL`               | `https://api.cursor.com/v1`     | Provider base URL                            |
| `LLM_REQUEST_TIMEOUT_S`      | `300`                           | HTTP request timeout                         |
| `LLM_POLL_INTERVAL_S`        | `1.0`                           | Cursor run poll interval                     |
| `LLM_POLL_TIMEOUT_S`         | `300`                           | Max time waiting for a run                   |
| `STRUCTURED_OUTPUT_MAX_RETRIES` | `1`                          | JSON repair retries per attempt              |
| `MAX_REVIEW_ROUNDS`          | `1`                             | Reviewer runs at most once per workflow      |
| `MAX_ARTIFACT_REVISIONS`     | `1`                             | Max regenerations per artifact per workflow  |
| `MAX_LLM_RETRIES`            | `1`                             | Bounded retries for transient provider errors |

`get_settings()` (cached) also creates `data`, `data/runs`, and
`data/artifacts` on first call and raises `RuntimeError` if required API keys
are missing. Gemini additionally rejects duplicate agent keys.

---

## Running the System

### 1. Interactive CLI demo

```bash
python -m agentic_core.cli
```

Walks the exact demo flow: idea → discovery Q&A → summary → confirm →
autonomous engineering with live progress → rendered artifact list.

### 2. Scripted end-to-end demo (real Cursor API)

```bash
python -m scripts.demo_football
```

Runs a pre-scripted conversation for a **football field booking platform** end
to end against the live provider, prints live progress, and writes artifacts
under `data/artifacts/<project_id>/`. Exits `0` on approval, `1` otherwise.

### 3. Headless end-to-end test (real Cursor API)

```bash
python -m scripts.run_test "YOUR BUSINESS IDEA"
```

Auto-answers discovery questions (no stdin needed), runs the full engineering
workflow against the live provider, renders artifacts, then prints a per-agent
cost table (wall-clock + TTFT + estimated in/out tokens) and the total LLM call
count, read from the run tracker. Useful for measuring speed/token changes.

### 4. REST API server

```bash
uvicorn agentic_core.api.app:app --host 0.0.0.0 --port 8000
```

Then drive it from any HTTP client:

```bash
# Create project + first discovery turn
curl -X POST http://localhost:8000/api/projects \
  -H "Content-Type: application/json" \
  -d '{"business_idea": "I want to build a platform where users can book football fields."}'

# Answer a discovery question
curl -X POST http://localhost:8000/api/projects/<PROJECT_ID>/discovery/message \
  -H "Content-Type: application/json" \
  -d '{"message": "Players, field owners and admins."}'

# Confirm when status == ready_for_confirmation
curl -X POST http://localhost:8000/api/projects/<PROJECT_ID>/discovery/confirm

# Start generation, then stream progress
curl -X POST http://localhost:8000/api/projects/<PROJECT_ID>/generate
curl -N http://localhost:8000/api/projects/<PROJECT_ID>/generation/status

# Fetch rendered artifacts
curl http://localhost:8000/api/projects/<PROJECT_ID>/artifacts
curl http://localhost:8000/api/projects/<PROJECT_ID>/artifacts/overview.md
```

Interactive API docs are available at `http://localhost:8000/docs` (FastAPI
auto-generated Swagger UI).

---

## Running Tests

```bash
pytest
```

The suite is fully hermetic — it uses `FakeLLMProvider` (`tests/helpers.py` has
valid sample outputs per agent plus a `build_handler()` that routes each call to
the right response). `pytest.ini` sets `asyncio_mode = auto` and
`testpaths = tests`. Notable coverage:

- `test_agents.py` — structured output success, JSON repair recovery, persistent
  failure, provider errors, and that each agent receives its dependency inputs.
- `test_discovery.py` — clarify/ready transitions, confirmation gating,
  transcript history, last-answer-wins, idempotent field application.
- `test_orchestrator.py` — execution order, dependency feeding, single-review
  round + targeted regeneration, revision limits, failed-revision artifact
  preservation, blocking-only regeneration, agent-failure stopping, event
  emission, run tracking and call-count reporting.
- `test_e2e.py` — a full food-delivery workflow from idea to approved blueprint
  with the complete artifact set.

---

## Extending the System

### Add a new agent

1. Create the prompt in `prompts/<name>.py` and register it in the `PROMPTS`
   dict in `prompts/__init__.py`.
2. Create the output schema in `schemas/<name>.py` and export it from
   `schemas/__init__.py`.
3. Create `agents/<name>.py` with a class extending `BaseAgent` (set `name`,
   `system_prompt`, `output_schema`, implement `_execute`), and add it to
   `agents/__init__.py` `build_agents()`.
4. Add it to `ENGINEERING_ORDER` and `DEPENDENTS` in the orchestrator if it is
   part of the linear pipeline, and feed it its dependencies in `_execute`.
5. Add sample output + a marker to `tests/helpers.py` and a test file.

### Swap the LLM provider

Implement `LLMProvider.generate()` and register it in `llm/__init__.py`. For
providers that need per-agent credentials, return a named service mapping from
`create_agent_llm_services()`.

### Add a rendered artifact

Add a `render_*` function in `artifacts/render.py`, call it from
`render_all()`, and it will automatically be persisted by the API generation
task and listed under artifacts.

---

## Security Notes

- **Secrets** live only in `.env` / environment variables. `.env.example` is
  the template; never commit your real `.env`.
- The `LLMProvider` and tracker never log API keys or secrets.
- DevOps artifacts are generated for review only and are **never executed**
  automatically (stated explicitly in the DevOps prompt).
- `ArtifactStore._safe_name` strips path separators to prevent path-traversal
  on artifact names.
- The FastAPI CORS middleware currently allows all origins — appropriate for a
  hackathon demo, but restrict it before production use.