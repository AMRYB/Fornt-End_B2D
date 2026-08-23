"""Central LLM service.

Agents call ``LLMService.generate`` and never touch a provider/SDK directly.
The service is responsible for structured output: it embeds the target JSON
schema in the prompt, parses the assistant text, validates it with Pydantic
and performs a bounded number of repair retries before failing.
"""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Iterator
from typing import TypeVar

from pydantic import BaseModel, ValidationError

from ..config import Settings
from .base import (
    LLMGenerationError,
    LLMProvider,
    LLMProviderError,
    StructuredOutputError,
)

T = TypeVar("T", bound=BaseModel)

_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.DOTALL)


def _try_parse(text: str) -> dict | None:
    """Parse *text* as a JSON object, returning ``None`` on any failure."""
    if not text or not text.strip():
        return None
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _balanced_region(text: str, start: int) -> str | None:
    """Return the ``{...}`` region that balances at *start* (string-aware).

    Returns ``None`` if the object never closes. Braces inside quoted strings
    are ignored so JSON embedded in prose is found correctly.
    """
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return text[start : i + 1]
    return None


def _candidate_regions(text: str) -> Iterator[str]:
    """Yield candidate JSON object substrings, largest (outermost) first."""
    regions: list[str] = []
    for start in (i for i, ch in enumerate(text) if ch == "{"):
        region = _balanced_region(text, start)
        if region is not None:
            regions.append(region)
    regions.sort(key=len, reverse=True)
    seen: set[str] = set()
    for region in regions:
        if region in seen:
            continue
        seen.add(region)
        yield region
        repaired = _strip_trailing_commas(region)
        if repaired != region and repaired not in seen:
            seen.add(repaired)
            yield repaired


def _strip_trailing_commas(text: str) -> str:
    """Remove trailing commas before ``}``/``]``, ignoring commas in strings."""
    result: list[str] = []
    in_string = False
    escaped = False
    pending_comma = False
    pending_ws: list[str] = []
    for ch in text:
        if in_string:
            result.append(ch)
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if pending_comma:
            if ch == ",":
                continue  # collapse duplicate commas
            if ch.isspace():
                pending_ws.append(ch)
                continue
            if ch in "}]":
                pending_comma = False
                pending_ws = []
                result.append(ch)
                continue
            # Real content follows the comma: emit it plus any buffered space.
            result.append(",")
            result.extend(pending_ws)
            pending_comma = False
            pending_ws = []
        if ch == '"':
            result.append(ch)
            in_string = True
            continue
        if ch == ",":
            pending_comma = True
            continue
        result.append(ch)
    if pending_comma:
        result.append(",")
        result.extend(pending_ws)
    return "".join(result)


def extract_json_object(text: str) -> dict:
    """Extract a single JSON object from an assistant response.

    Tolerates surrounding prose, fenced code blocks, nested/multiple objects
    and common LLM mistakes such as trailing commas. Raises
    :class:`StructuredOutputError` with a preview of the offending text when no
    object can be recovered.
    """
    candidate = text.strip()
    if not candidate:
        raise StructuredOutputError(
            "Could not extract a JSON object from the model response: response was empty"
        )

    direct = _try_parse(candidate)
    if direct is not None:
        return direct

    for match in _FENCE_RE.finditer(candidate):
        fenced = _try_parse(match.group(1).strip())
        if fenced is not None:
            return fenced

    for region in _candidate_regions(candidate):
        parsed = _try_parse(region)
        if parsed is not None:
            return parsed

    preview = re.sub(r"\s+", " ", candidate)[:200]
    raise StructuredOutputError(
        "Could not extract a JSON object from the model response. "
        f"Raw text preview: {preview!r}"
    )


class LLMService:
    def __init__(self, provider: LLMProvider, settings: Settings | None = None):
        self._provider = provider
        self._max_retries = (
            settings.structured_output_max_retries if settings else 1
        )
        # If the initial generation already consumed most of the provider's
        # poll-timeout budget, a repair round-trip is very likely to time out
        # too, so fail fast instead of doubling the wall-clock cost.
        self._repair_skip_threshold_s = (
            settings.llm_poll_timeout_s * 0.8 if settings and settings.llm_poll_timeout_s else None
        )

    @property
    def provider(self) -> LLMProvider:
        """Provider owned by this service, exposed for application lifecycle cleanup."""
        return self._provider

    async def generate(
        self,
        system_prompt: str,
        user_prompt: str,
        schema: type[T] | None = None,
        stats: dict[str, int] | None = None,
    ) -> T | str:
        """Generate a response, optionally enforced against a Pydantic schema.

        ``stats["repair_count"]`` is updated with the number of repair retries.
        """
        if stats is None:
            stats = {}
        if schema is None:
            stats["prompt_chars"] = stats.get("prompt_chars", 0) + len(system_prompt) + len(user_prompt)
            raw = await self._provider.generate(system_prompt, user_prompt, stats)
            stats["output_chars"] = stats.get("output_chars", 0) + len(raw)
            return raw
        return await self._generate_structured(system_prompt, user_prompt, schema, stats)

    async def _generate_structured(
        self,
        system_prompt: str,
        user_prompt: str,
        schema: type[T],
        stats: dict[str, int],
    ) -> T:
        full_prompt = self._with_schema(user_prompt, schema)
        stats["prompt_chars"] = stats.get("prompt_chars", 0) + len(system_prompt) + len(full_prompt)
        loop = asyncio.get_running_loop()
        started = loop.time()
        raw = await self._provider.generate(system_prompt, full_prompt, stats)
        attempts = 0
        while True:
            try:
                validated = self._validate(raw, schema)
                stats["output_chars"] = stats.get("output_chars", 0) + len(raw)
                return validated
            except (StructuredOutputError, ValidationError) as exc:
                if attempts >= self._max_retries:
                    raise StructuredOutputError(
                        f"Agent could not produce a valid response after "
                        f"{attempts + 1} attempt(s): {exc}"
                    ) from exc
                elapsed = loop.time() - started
                if (
                    self._repair_skip_threshold_s is not None
                    and elapsed >= self._repair_skip_threshold_s
                ):
                    raise StructuredOutputError(
                        f"Agent output was invalid and repair was skipped because the "
                        f"initial generation already ran for {elapsed:.0f}s: {exc}"
                    ) from exc
                raw = await self._repair(system_prompt, full_prompt, schema, raw, exc, stats)
                attempts += 1
                stats["repair_count"] = attempts

    def _validate(self, raw: str, schema: type[T]) -> T:
        data = extract_json_object(raw)
        return schema.model_validate(data)

    async def _repair(
        self,
        system_prompt: str,
        user_prompt: str,
        schema: type[T],
        raw: str,
        error: Exception,
        stats: dict[str, int],
    ) -> str:
        # The previous response can be huge (the model sometimes rambles before
        # the JSON). Re-sending all of it on every repair doubles input tokens,
        # so cap it — the schema and error are the operative context.
        previous = raw.strip()
        if len(previous) > 4000:
            previous = previous[:4000] + f"\n... ({len(raw) - 4000} chars omitted ...)"
        repair_prompt = (
            f"{user_prompt}\n\n"
            f"Your previous response was rejected because it did not contain a "
            f"valid JSON object matching the schema.\n\n"
            f"Previous response (trimmed):\n{previous}\n\n"
            f"Validation error:\n{error}\n\n"
            f"Reply again with ONLY a single valid JSON object matching the "
            f"schema above. No markdown, no code fences, no commentary."
        )
        stats["prompt_chars"] = stats.get("prompt_chars", 0) + len(system_prompt) + len(repair_prompt)
        try:
            return await self._provider.generate(system_prompt, repair_prompt, stats)
        except LLMProviderError as exc:
            raise LLMGenerationError(f"Repair request failed: {exc}") from exc

    def _with_schema(self, user_prompt: str, schema: type[T]) -> str:
        schema_json = json.dumps(self._schema_for(schema), indent=2)
        return (
            f"{user_prompt}\n\n"
            f"JSON OUTPUT REQUIREMENTS\n"
            f"Reply with ONLY a single valid JSON object — no markdown, no code "
            f"fences, no commentary. The object must conform exactly to this "
            f"JSON Schema:\n\n{schema_json}"
        )

    def _schema_for(self, schema: type[T]) -> dict:
        """Return the JSON schema sent to the LLM.

        Fields listed in the schema class' ``llm_exclude_fields`` are dropped
        so the model is never asked to (or allowed to) produce output that the
        system derives locally instead — a guaranteed reduction in output tokens.
        """
        spec = schema.model_json_schema()
        excluded = getattr(schema, "llm_exclude_fields", frozenset())
        if excluded:
            props = spec.get("properties", {})
            for name in excluded:
                props.pop(name, None)
            spec["required"] = [r for r in spec.get("required", []) if r not in excluded]
        return spec