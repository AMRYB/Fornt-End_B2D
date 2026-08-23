"""Helpers: valid sample outputs per agent + an agent-detecting handler."""

from __future__ import annotations

import json

AGENT_MARKERS: list[tuple[str, str]] = [
    ("discovery", "Discovery Agent"),
    ("requirements", "Requirements Engineer agent"),
    ("architecture", "Architecture agent"),
    ("database", "Database Design agent"),
    ("api", "API Design agent"),
    ("devops", "DevOps Engineer agent"),
    ("reviewer", "Review Agent"),
]


def detect_agent(system_prompt: str) -> str:
    for name, marker in AGENT_MARKERS:
        if marker in system_prompt:
            return name
    return "unknown"


def discovery_output(status: str = "ready") -> dict:
    return {
        "status": status,
        "confidence": 0.94 if status == "ready" else 0.6,
        "summary": "A platform where users order food from local restaurants and track delivery.",
        "known_information": {
            "problem": "Ordering food takes too long",
            "target_users": ["Customers", "Restaurants"],
            "user_roles": ["Customer", "Restaurant Owner", "Admin"],
            "business_goals": ["Increase orders", "Fast delivery"],
            "core_features": [
                "Restaurant discovery",
                "Order placement",
                "Online payment",
                "Delivery tracking",
            ],
            "payment_requirement": "Online payment",
            "notification_requirement": "Order status notifications",
        },
        "missing_information": [] if status == "ready" else [
            {"field": "user_roles", "importance": "critical", "reason": "Required to define authorization"}
        ],
        "questions": [] if status == "ready" else [
            {"id": "q1", "question": "Who are the main users of the platform?", "reason": "Defines roles and permissions."}
        ],
    }


def requirements_output() -> dict:
    return {
        "functional_requirements": [
            "FR1: Users can browse restaurants and menus.",
            "FR2: Users can place orders and pay online.",
            "FR3: Users can track delivery status.",
        ],
        "non_functional_requirements": [
            "NFR1: The system must respond within 500ms for core operations.",
            "NFR2: Payment data must be encrypted in transit and at rest.",
        ],
        "user_stories": [
            "As a customer, I want to order food online, so that I save time.",
            "As a restaurant owner, I want to receive orders, so that I can fulfill them.",
        ],
        "acceptance_criteria": [
            "AC1: A customer can complete an order end-to-end.",
            "AC2: Payment failures return a clear error.",
        ],
        "constraints": ["Must run on commodity cloud infrastructure."],
        "assumptions": ["Payment provider is Stripe."],
    }


def architecture_output() -> dict:
    return {
        "system_components": [
            {"name": "Web Frontend", "type": "frontend", "description": "React SPA", "technology": "React"},
            {"name": "API Backend", "type": "backend", "description": "REST API", "technology": "FastAPI (Python)"},
            {"name": "Database", "type": "database", "description": "Primary datastore", "technology": "PostgreSQL"},
            {"name": "Payments", "type": "external", "description": "Payment processing", "technology": "Stripe"},
        ],
        "communication": ["Frontend calls the API over HTTPS", "API persists to PostgreSQL"],
        "authentication": "JWT bearer tokens",
        "security": ["TLS everywhere", "Rate limiting on auth endpoints"],
        "scalability": ["Horizontal scaling of the API behind a load balancer"],
        "technology_stack": {
            "frontend": "React",
            "backend": "FastAPI (Python)",
            "database": "PostgreSQL",
            "payments": "Stripe",
        },
        "deployment_architecture": "Containerized services on a single cloud VM, scaled out as needed.",
        "mermaid_diagram": "flowchart TD\n  FE[Web Frontend] --> API[API Backend]\n  API --> DB[(PostgreSQL)]\n  API --> PS[Stripe]",
    }


def database_output() -> dict:
    return {
        "database_technology": "PostgreSQL",
        "entities": [
            {
                "name": "users",
                "description": "Platform users",
                "fields": [
                    {"name": "id", "type": "uuid", "primary_key": True, "nullable": False, "unique": False, "indexed": False, "foreign_key": None},
                    {"name": "email", "type": "varchar", "primary_key": False, "nullable": False, "unique": True, "indexed": True, "foreign_key": None},
                    {"name": "role", "type": "varchar", "primary_key": False, "nullable": False, "unique": False, "indexed": False, "foreign_key": None},
                ],
            },
            {
                "name": "orders",
                "description": "Food orders",
                "fields": [
                    {"name": "id", "type": "uuid", "primary_key": True, "nullable": False, "unique": False, "indexed": False, "foreign_key": None},
                    {"name": "user_id", "type": "uuid", "primary_key": False, "nullable": False, "unique": False, "indexed": True, "foreign_key": "users.id"},
                    {"name": "status", "type": "varchar", "primary_key": False, "nullable": False, "unique": False, "indexed": False, "foreign_key": None},
                ],
            },
        ],
        "relationships": ["orders.user_id references users.id"],
        "indexes": ["idx_orders_user_id ON orders(user_id)"],
        "constraints": ["orders.status CHECK in ('pending','paid','delivered')"],
        "sql_schema": "CREATE TABLE users (id uuid PRIMARY KEY, email varchar NOT NULL UNIQUE, role varchar NOT NULL);\nCREATE TABLE orders (id uuid PRIMARY KEY, user_id uuid NOT NULL REFERENCES users(id), status varchar NOT NULL);",
        "erd_mermaid": "erDiagram\n  users ||--o{ orders : places",
    }


def api_output() -> dict:
    return {
        "endpoints": [
            {
                "method": "GET",
                "path": "/api/restaurants",
                "summary": "List restaurants",
                "auth": "none",
                "pagination": True,
                "filters": ["cuisine"],
                "request_schema": None,
                "response_schema": {"type": "object", "properties": {"items": {"type": "array"}}},
            },
            {
                "method": "POST",
                "path": "/api/orders",
                "summary": "Place an order",
                "auth": "jwt",
                "pagination": False,
                "filters": [],
                "request_schema": {"type": "object", "properties": {"restaurant_id": {"type": "string"}}},
                "response_schema": {"type": "object", "properties": {"id": {"type": "string"}}},
            },
            {
                "method": "GET",
                "path": "/api/orders/{id}",
                "summary": "Get an order",
                "auth": "jwt",
                "pagination": False,
                "filters": [],
                "request_schema": None,
                "response_schema": {"type": "object", "properties": {"status": {"type": "string"}}},
            },
        ],
        "authentication": "JWT bearer tokens",
        "authorization": "Customers access their own orders; owners access their restaurant data",
        "error_handling": ["Standard error envelope with code/message/request_id", "404 for missing resources"],
        "pagination": "cursor-based",
        "filtering": "query parameters",
        "openapi_spec": {"openapi": "3.0.0", "info": {"title": "Food Delivery API", "version": "1.0.0"}, "paths": {}},
    }


def devops_output() -> dict:
    return {
        "dockerfile": "FROM python:3.11-slim\nWORKDIR /app\nCOPY requirements.txt .\nRUN pip install -r requirements.txt\nCOPY . .\nCMD [\"uvicorn\", \"app.main:app\", \"--host\", \"0.0.0.0\"]",
        "docker_compose": "services:\n  api:\n    build: .\n    ports: ['8000:8000']\n  db:\n    image: postgres:16",
        "ci_cd_pipeline": "Lint, test, build image, push to registry, deploy to VM",
        "github_actions": "name: CI\non: [push]\njobs:\n  test:\n    runs-on: ubuntu-latest\n    steps:\n      - uses: actions/checkout@v4",
        "environment_variables": {"DATABASE_URL": "postgres://user:pass@db:5432/app", "STRIPE_SECRET_KEY": "${STRIPE_SECRET_KEY}"},
        "deployment_strategy": "Docker Compose on a single VM with rolling container restarts",
        "health_checks": ["/health endpoint on API", "pg_isready for database"],
        "logging": ["Structured JSON logs to stdout"],
        "monitoring": ["Prometheus metrics on /metrics"],
        "secrets_management": "Secrets injected via environment variables from the CI/CD provider secret store",
    }


def review_output(status: str = "approved") -> dict:
    if status == "approved":
        return {"status": "approved", "score": 0.96, "issues": [], "artifacts_to_regenerate": []}
    return {
        "status": "needs_revision",
        "score": 0.7,
        "issues": [
            {
                "artifact": "database",
                "severity": "blocking",
                "problem": "Database technology conflicts with architecture.",
                "expected": "PostgreSQL",
                "actual": "MongoDB",
                "fix": "Switch database_technology to PostgreSQL.",
                "source_artifact": "architecture",
                "source_decision": "Database component technology: PostgreSQL",
                "conflicting_artifact": "database",
                "conflicting_decision": "database_technology: MongoDB",
            }
        ],
        "artifacts_to_regenerate": ["database"],
    }


def review_output_targets(targets: list[str]) -> dict:
    """A needs_revision review flagging ``targets`` as blocking."""
    issues = [
        {
            "artifact": target,
            "severity": "blocking",
            "problem": f"{target} artifact is inconsistent.",
            "expected": "expected value",
            "actual": "actual value",
            "fix": "fix it",
            "source_artifact": "database",
            "source_decision": "decision",
            "conflicting_artifact": target,
            "conflicting_decision": "decision",
        }
        for target in targets
    ]
    return {
        "status": "needs_revision",
        "score": 0.6,
        "issues": issues,
        "artifacts_to_regenerate": targets,
    }


def build_handler(discovery_status: str = "ready", review_status: str = "approved", review_sequence: list[str] | None = None):
    """Return a handler that answers every agent with valid output.

    ``review_sequence`` overrides review answers call-by-call when provided.

    A revision call (user prompt contains ``REVISION TASK``) returns a *changed*
    artifact so the orchestrator's hash-based convergence check sees a real
    change (as the real LLM would produce).
    """
    review_index = 0
    reviews = list(review_sequence) if review_sequence else None

    def handler(system_prompt: str, user_prompt: str) -> str:
        nonlocal review_index
        agent = detect_agent(system_prompt)
        if agent == "discovery":
            return json.dumps(discovery_output(discovery_status))
        if agent == "requirements":
            base = requirements_output()
            return json.dumps(revised_output(agent, base) if "REVISION TASK" in user_prompt else base)
        if agent == "architecture":
            base = architecture_output()
            return json.dumps(revised_output(agent, base) if "REVISION TASK" in user_prompt else base)
        if agent == "database":
            base = database_output()
            return json.dumps(revised_output(agent, base) if "REVISION TASK" in user_prompt else base)
        if agent == "api":
            base = api_output()
            return json.dumps(revised_output(agent, base) if "REVISION TASK" in user_prompt else base)
        if agent == "devops":
            base = devops_output()
            return json.dumps(revised_output(agent, base) if "REVISION TASK" in user_prompt else base)
        if agent == "reviewer":
            if reviews is not None:
                answer = reviews[min(review_index, len(reviews) - 1)]
                review_index += 1
                return json.dumps(review_output(answer))
            return json.dumps(review_output(review_status))
        raise AssertionError(f"Unknown agent marker in: {system_prompt[:80]}")

    return handler


def revised_output(agent: str, base: dict) -> dict:
    """Return a *changed* artifact for a revision run, so the orchestrator's
    hash-based convergence check sees the artifact actually changed."""
    out = dict(base)
    if agent == "database":
        out["constraints"] = list(base.get("constraints") or []) + ["review_revision_applied"]
    elif agent == "api":
        out["authentication"] = (base.get("authentication") or "") + " (revised)"
    elif agent == "devops":
        out["deployment_strategy"] = (base.get("deployment_strategy") or "") + " (revised)"
    elif agent == "architecture":
        out["deployment_architecture"] = (base.get("deployment_architecture") or "") + " (revised)"
    elif agent == "requirements":
        out["constraints"] = list(base.get("constraints") or []) + ["review_revision_applied"]
    return out