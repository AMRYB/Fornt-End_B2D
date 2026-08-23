"""Headless end-to-end test: idea -> discovery -> engineering -> review.

Answers discovery questions automatically (no stdin) so the whole workflow can
run unattended against the real Cursor provider. Prints a per-agent cost table
(duration + estimated tokens) read from the ExecutionTracker.

Run:  python -m scripts.run_test "YOUR BUSINESS IDEA"
"""

from __future__ import annotations

import asyncio
import sys

from agentic_core.artifacts import ArtifactStore, render_all
from agentic_core.config import get_settings
from agentic_core.llm import aclose_llm_services, create_agent_llm_services
from agentic_core.orchestrator import DiscoveryError, EventBus, ExecutionTracker, Orchestrator
from agentic_core.project_store import ProjectStore

MAX_DISCOVERY_ROUNDS = 8


def auto_answer(question) -> str:
    """Answer any discovery question: pick the first option when available,
    otherwise fall back to a definitive, concrete reply. This exercises the
    multiple-choice path end to end."""
    if getattr(question, "options", None):
        return question.options[0]
    return (
        "v1 ships as a responsive web app that works on mobile and desktop browsers "
        "in Cairo, Egypt only. Owners find groomers by neighborhood, service type, "
        "price, availability and ratings; every groomer must provide a profile with "
        "address, services, prices, photos, working hours and pet types. Booking is a "
        "request that the groomer must accept before the slot is reserved and the "
        "owner's card is charged. We use Paymob to process Egypt payments: the platform "
        "holds the charge and pays the groomer 90% within 24 hours after the appointment, "
        "keeping a 10% commission. Owners can cancel free up to 24 hours before the "
        "appointment and get a full refund; if they cancel inside 24 hours or do not "
        "drop the dog off, the full charge is kept and the groomer still gets paid. "
        "Appointment reminders go to both owners and groomers by email and SMS, 24 hours "
        "and 2 hours before the appointment. Accounts use email + password with "
        "role-based access for owners, groomers and admins."
    )


async def run(idea: str) -> None:
    settings = get_settings()
    llm_services = create_agent_llm_services(settings)
    try:
        await _run(idea, settings, llm_services)
    finally:
        await aclose_llm_services(llm_services)


async def _run(idea: str, settings, llm_services) -> None:
    event_bus = EventBus()
    tracker = ExecutionTracker(settings.runs_dir)
    orchestrator = Orchestrator(llm_services, event_bus, tracker, settings)
    project_store = ProjectStore(settings.db_path, legacy_dir=settings.projects_dir)

    context = project_store.create(idea)
    print(f"[project {context.project_id}] {idea}\n")

    output = await orchestrator.discovery_turn(context, idea)
    rounds = 1
    while output.status != "ready" and rounds < MAX_DISCOVERY_ROUNDS:
        if not output.questions:
            # Agent needs more but asked nothing; nudge it to proceed or go ready.
            context.add_turn("user", "Please continue.")
            output = await orchestrator.discovery_turn(context)
            rounds += 1
            continue
        print(f"[discovery round {rounds}] {len(output.questions)} question(s):")
        for q in output.questions:
            print(f"  - {q.question}")
            context.add_turn("user", auto_answer(q))
        output = await orchestrator.discovery_turn(context)
        rounds += 1

    if output.status != "ready":
        print("Discovery did not reach 'ready'; aborting.")
        return

    print(f"\nDiscovered after {rounds} round(s). Summary: {output.summary}\n")

    orchestrator.confirm(context)
    results = await orchestrator.generate(context)
    project_store.save(context)

    if context.status in ("approved", "revised"):
        files = render_all(context)
        artifact_store = ArtifactStore(settings.artifacts_dir)
        for name, content in files.items():
            artifact_store.write(context.project_id, name, content)
        print(f"\nArtifacts ({len(files)}): {settings.artifacts_dir / context.project_id}")
        for name in sorted(files):
            print(f"  - {name}")
    else:
        print(f"\nWorkflow finished with status: {context.status}")

    _print_call_summary(results)
    _print_summary(tracker, context.project_id)


def _print_call_summary(results: dict) -> None:
    counts = results.get("call_counts", {})
    revisions = results.get("revisions", {})
    if not counts:
        return
    order = ["requirements", "architecture", "database", "api", "devops", "reviewer"]
    print("\n" + "=" * 78)
    print("TOTAL LLM CALLS")
    print("=" * 78)
    total = 0
    for agent in order:
        n = counts.get(agent, 0)
        total += n
        revision = f" (revised x{revisions.get(agent, 0)})" if revisions.get(agent, 0) else ""
        print(f"  {agent:<14} {n}{revision}")
    print(f"  {'TOTAL':<14} {total}")


def _print_summary(tracker, project_id: str) -> None:
    records = tracker.list(project_id)
    rows = [r for r in records if r.status == "success"]
    print("\n" + "=" * 108)
    print(f"{'agent':<14}{'status':<10}{'ms':>8}{'ttft s':>8}{'in tok':>12}{'out tok':>12}{'model':<20}")
    print("-" * 108)
    total_ms = total_in = total_out = 0
    for r in rows:
        ms = r.duration_ms or 0
        t_in = r.input_tokens or (r.input_chars // 4)
        t_out = r.output_tokens or (r.output_chars // 4)
        total_ms += ms
        total_in += t_in
        total_out += t_out
        print(f"{r.agent:<14}{r.status:<10}{ms:>8}{r.ttft_s or 0.0:>8.1f}{t_in:>12,}{t_out:>12,}{(r.model or '')[:20]:<20}")
    print("-" * 108)
    print(f"{'TOTAL':<14}{'':<10}{total_ms:>8}{'':>8}{total_in:>12,}{total_out:>12,}")
    print(f"\nTotal wall-clock (engineering agents only): {total_ms / 1000:.1f}s")
    print(f"Estimated tokens (chars/4): ~{total_in + total_out:,}")


if __name__ == "__main__":
    idea = " ".join(sys.argv[1:]) or "A marketplace connecting dog groomers with pet owners for bookings, reminders, and online payment."
    try:
        asyncio.run(run(idea))
    except KeyboardInterrupt:
        print("\nBye.")