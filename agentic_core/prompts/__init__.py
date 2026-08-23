"""Prompt registry.

Each agent has a static system prompt and a user-prompt template. Templates use
``{__KEY__}`` placeholders substituted by the agents at runtime via
:func:`build_user_prompt`.
"""

from __future__ import annotations

from dataclasses import dataclass

from . import (
    api,
    architecture,
    database,
    devops,
    discovery,
    requirements,
    reviewer,
)


@dataclass(frozen=True)
class Prompt:
    name: str
    system: str
    user_template: str


PROMPTS: dict[str, Prompt] = {
    "discovery": Prompt("discovery", discovery.SYSTEM_PROMPT, discovery.USER_TEMPLATE),
    "requirements": Prompt(
        "requirements", requirements.SYSTEM_PROMPT, requirements.USER_TEMPLATE
    ),
    "architecture": Prompt(
        "architecture", architecture.SYSTEM_PROMPT, architecture.USER_TEMPLATE
    ),
    "database": Prompt("database", database.SYSTEM_PROMPT, database.USER_TEMPLATE),
    "api": Prompt("api", api.SYSTEM_PROMPT, api.USER_TEMPLATE),
    "devops": Prompt("devops", devops.SYSTEM_PROMPT, devops.USER_TEMPLATE),
    "reviewer": Prompt("reviewer", reviewer.SYSTEM_PROMPT, reviewer.USER_TEMPLATE),
}


def build_user_prompt(name: str, **values: str) -> str:
    prompt = PROMPTS[name]
    text = prompt.user_template
    for key, value in values.items():
        text = text.replace(f"{{__{key.upper()}__}}", value)
    return text