"""Interactive CLI demo of the agentic system.

Run with ``python -m agentic_core.cli``. Walks the exact demo flow:
vague idea -> discovery questions -> summary -> confirm -> autonomous
engineering with live progress -> rendered artifacts.
"""

from __future__ import annotations

import asyncio

from .artifacts import ArtifactStore, render_all
from .config import get_settings
from .llm import aclose_llm_services, create_agent_llm_services
from .orchestrator import DiscoveryError, EventBus, Orchestrator
from .project_store import ProjectStore


def parse_user_answer(raw: str, options: list[str]) -> str:
    """Turn a CLI answer into text: option numbers become their option text,
    anything else is used verbatim (the user's own answer)."""
    text = raw.strip()
    if not options or not text:
        return text
    parts = [p.strip() for p in text.replace(",", " ").split() if p.strip()]
    if parts and all(p.isdigit() for p in parts):
        picked = [options[int(p) - 1] for p in parts if 1 <= int(p) <= len(options)]
        if picked:
            return "; ".join(picked)
    return text


async def _wait_with_progress(coro):
    """Await *coro* while printing a heartbeat so long agent runs don't feel stuck."""
    task = asyncio.create_task(coro)
    while not task.done():
        await asyncio.sleep(5)
        print(".", end="", flush=True)
    print()
    return task.result()


def _print_event(event) -> None:
    symbols = {
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
    symbol = symbols.get(event.event, "•")
    label = event.agent or event.event
    detail = f" — {event.reason}" if event.reason else ""
    if event.invocation is not None and event.invocation > 1:
        detail += f" [invocation #{event.invocation}]"
    if event.duration_ms is not None:
        detail += f" ({event.duration_ms}ms)"
    if event.input_chars is not None:
        detail += f" ~{event.input_chars // 4} tok in / ~{event.output_chars // 4} tok out"
    print(f"  {symbol} {label}{detail}")


def _print_call_summary(results: dict) -> None:
    counts = results.get("call_counts", {})
    revisions = results.get("revisions", {})
    if not counts:
        return
    order = ["requirements", "architecture", "database", "api", "devops", "reviewer"]
    print("\n" + "=" * 60)
    print("TOTAL LLM CALLS")
    print("=" * 60)
    total = 0
    for agent in order:
        n = counts.get(agent, 0)
        total += n
        revision = f" (revised x{revisions.get(agent, 0)})" if revisions.get(agent, 0) else ""
        print(f"  {agent:<14} {n}{revision}")
    print(f"  {'TOTAL':<14} {total}")


async def run() -> None:
    settings = get_settings()
    llm_services = create_agent_llm_services(settings)
    try:
        await _run(settings, llm_services)
    finally:
        await aclose_llm_services(llm_services)


async def _run(settings, llm_services) -> None:
    event_bus = EventBus()
    orchestrator = Orchestrator(llm_services, event_bus, None, settings)
    project_store = ProjectStore(settings.db_path, legacy_dir=settings.projects_dir)

    print("=" * 60)
    print("Agentic AI Core — Business Idea to Engineering Blueprint")
    print("=" * 60)

    try:
        idea = input("\nDescribe your business idea: ").strip()
    except (EOFError, KeyboardInterrupt):
        print("\nBye.")
        return
    if not idea:
        print("No idea provided. Exiting.")
        return

    context = project_store.create(idea)
    print(f"\n[project {context.project_id}] Starting discovery…\n")

    try:
        print("Analyzing your idea (can take a minute)…", end="", flush=True)
        output = await _wait_with_progress(orchestrator.discovery_turn(context, idea))
    except DiscoveryError as exc:
        print(f"Discovery failed: {exc}")
        return

    while output.status != "ready":
        if not output.questions:
            # Agent says more info is needed but asked nothing: nudge it once
            # instead of looping forever.
            print("  (agent needs a bit more detail — nudging it to proceed)")
            context.add_turn("user", "Please continue.")
            print("Updating understanding…", end="", flush=True)
            try:
                output = await _wait_with_progress(orchestrator.discovery_turn(context))
            except DiscoveryError as exc:
                print(f"Discovery failed: {exc}")
                return
            continue
        for idx, question in enumerate(output.questions, 1):
            print(f"\n{idx}. {question.question}  ({question.reason})")
            if question.options:
                for j, option in enumerate(question.options, 1):
                    print(f"     {j}) {option}")
        answers = []
        for question in output.questions:
            hint = "  (pick a number, several like 1,3, or type your own)" if question.options else ""
            print(hint)
            try:
                raw = input("\n> ")
            except (EOFError, KeyboardInterrupt):
                print("\nBye.")
                return
            answer = parse_user_answer(raw, question.options)
            if not answer:
                print("  (empty answer ignored — type something or pick an option so discovery can continue)")
                answers.append(None)
            else:
                answers.append(answer)
        real_answers = [a for a in answers if a]
        if not real_answers:
            print("  (no answers provided — nothing sent to discovery)")
            continue
        # Batch every answer into a single discovery run: one turn instead of
        # one Cursor run per question, cutting discovery cost dramatically.
        for answer in real_answers:
            context.add_turn("user", answer)
        print("Updating understanding…", end="", flush=True)
        try:
            output = await _wait_with_progress(orchestrator.discovery_turn(context))
        except DiscoveryError as exc:
            print(f"Discovery failed: {exc}")
            return

    print("\n" + "=" * 60)
    print("YOUR PROJECT UNDERSTANDING")
    print("=" * 60)
    print(output.summary)
    print("\n--- Context ---")
    print(f"Problem:      {context.problem or '-'}")
    print(f"Users:        {', '.join(context.target_users) or '-'}")
    print(f"Roles:        {', '.join(context.user_roles) or '-'}")
    print(f"Goals:        {', '.join(context.business_goals) or '-'}")
    print(f"Features:     {', '.join(context.core_features) or '-'}")
    print(f"Constraints:  {', '.join(context.constraints) or '-'}")
    print(f"Integrations: {', '.join(context.integrations) or '-'}")
    print(f"Tech pref:    {', '.join(context.technology_preferences) or '-'}")

    try:
        confirm = input("\n[Confirm & Generate] (y/n): ").strip().lower()
    except (EOFError, KeyboardInterrupt):
        print("\nBye.")
        return
    if confirm not in ("y", "yes"):
        print("Generation cancelled.")
        return

    orchestrator.confirm(context)
    event_bus.subscribe(_print_event)
    print("\n" + "=" * 60)
    print("AUTONOMOUS ENGINEERING WORKFLOW")
    print("=" * 60)
    try:
        results = await orchestrator.generate(context)
    finally:
        event_bus.unsubscribe(_print_event)
    _print_call_summary(results)

    project_store.save(context)
    if context.status in ("approved", "revised"):
        print("\n" + "=" * 60)
        print("FINAL PROJECT BLUEPRINT")
        print("=" * 60)
        files = render_all(context)
        artifact_store = ArtifactStore(settings.artifacts_dir)
        for name, content in files.items():
            artifact_store.write(context.project_id, name, content)
        for name in sorted(files):
            print(f"  • {name}")
        print(f"\nArtifacts saved under: {settings.artifacts_dir / context.project_id}")
    else:
        print(f"\nWorkflow finished with status: {context.status}")


if __name__ == "__main__":
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        print("\nBye.")