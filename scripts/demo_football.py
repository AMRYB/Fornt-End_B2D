"""Scripted live demo: football field booking platform, end to end.

Runs the complete workflow against the real Cursor API:
discovery conversation -> confirmation -> autonomous engineering -> review
-> rendered artifacts under data/artifacts/<project_id>/.

Usage:  python -m scripts.demo_football
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

# Windows consoles default to cp1252, which cannot encode the demo's Unicode
# symbols (▶, ✓, ✗, …). Force UTF-8 so the demo never dies mid-print.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

from agentic_core.artifacts import ArtifactStore, render_all
from agentic_core.config import get_settings
from agentic_core.llm import aclose_llm_services, create_agent_llm_services
from agentic_core.orchestrator import EventBus, Orchestrator
from agentic_core.project_store import ProjectStore

SCRIPT = [
    "I want to build a platform where users can book football fields.",
    "Players and field owners, plus admins. Multiple independent venues list their own pitches.",
    "Players should pay online when booking; field owners receive the money minus a small platform commission.",
    "Yes, field owners manage their own field availability and pricing, and admins manage users and disputes.",
    "The first version covers finding fields, booking, online payment, and owner availability calendars.",
    "Players, owners and admins sign in with email and password.",
    "Bookings are confirmed by email and in-app; players can cancel up to 24 hours before kick-off for a full refund.",
]

SYMBOLS = {
    "workflow_started": "▶",
    "agent_started": "→",
    "agent_completed": "✓",
    "agent_retrying": "↻",
    "agent_failed": "✗",
    "review_started": "◈",
    "review_completed": "✓",
    "review_failed": "⚠",
    "workflow_completed": "✔",
    "workflow_failed": "✗",
}


def on_event(event) -> None:
    symbol = SYMBOLS.get(event.event, "•")
    label = event.agent or event.event
    detail = f" — {event.reason}" if event.reason else ""
    if event.invocation is not None and event.invocation > 1:
        detail += f" [invocation #{event.invocation}]"
    if event.duration_ms is not None:
        detail += f" ({event.duration_ms}ms)"
    print(f"  {symbol} {label}{detail}")


def print_call_summary(results: dict) -> None:
    counts = results.get("call_counts", {})
    revisions = results.get("revisions", {})
    if not counts:
        return
    order = ["requirements", "architecture", "database", "api", "devops", "reviewer"]
    print("\n" + "=" * 62)
    print("TOTAL LLM CALLS")
    print("=" * 62)
    total = 0
    for agent in order:
        n = counts.get(agent, 0)
        total += n
        revision = f" (revised x{revisions.get(agent, 0)})" if revisions.get(agent, 0) else ""
        print(f"  {agent:<14} {n}{revision}")
    print(f"  {'TOTAL':<14} {total}")


async def main() -> int:
    settings = get_settings()
    llm_services = create_agent_llm_services(settings)
    event_bus = EventBus()
    orchestrator = Orchestrator(llm_services, event_bus, None, settings)
    store = ProjectStore(settings.db_path, legacy_dir=settings.projects_dir)

    print("=" * 62)
    print("LIVE DEMO — Football Field Booking Platform")
    print("=" * 62)

    try:
        context = store.create(SCRIPT[0])
        print(f"\n[project {context.project_id}] discovery…\n")

        for message in SCRIPT:
            output = await orchestrator.discovery_turn(context, message)
            print(f"▶ USER: {message}")
            if output.questions:
                for question in output.questions:
                    print(f"❓ AGENT: {question.question}")
                    for j, option in enumerate(question.options, 1):
                        print(f"       {j}) {option}")
            print(f"  [status={output.status}, confidence={output.confidence:.2f}]")
            if output.status == "ready":
                break

        if context.status != "ready_for_confirmation":
            print("\nDiscovery did not reach 'ready' — run again with more answers.")
            return 1

        print("\n" + "=" * 62)
        print("YOUR PROJECT UNDERSTANDING")
        print("=" * 62)
        print(f"\nProblem:      {context.problem or '-'}")
        print(f"Users:        {', '.join(context.target_users) or '-'}")
        print(f"Roles:        {', '.join(context.user_roles) or '-'}")
        print(f"Features:     {', '.join(context.core_features) or '-'}")
        print(f"Integrations: {', '.join(context.integrations) or '-'}")
        print("\n→ Confirming & generating…\n")

        orchestrator.confirm(context)
        event_bus.subscribe(on_event)
        print("=" * 62)
        print("AUTONOMOUS ENGINEERING WORKFLOW")
        print("=" * 62)
        results = await orchestrator.generate(context)
        event_bus.unsubscribe(on_event)
        print_call_summary(results)

        store.save(context)
        files = render_all(context)
        artifact_dir = Path(settings.artifacts_dir) / context.project_id
        artifact_store = ArtifactStore(settings.artifacts_dir)
        for name, content in files.items():
            artifact_store.write(context.project_id, name, content)

        print("\n" + "=" * 62)
        print(f"FINAL STATE: {context.status}")
        if context.status in ("approved", "revised"):
            print("\nGenerated artifacts:")
            for name in sorted(files):
                print(f"  • {name}")
            print(f"\nSaved under: {artifact_dir}")
        return 0 if context.status in ("approved", "revised") else 1
    finally:
        await aclose_llm_services(llm_services)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))