"""Tests for structured-output extraction and repair behaviour."""

from __future__ import annotations

import asyncio
import json

import pytest

from agentic_core.agents import RequirementsAgent
from agentic_core.llm import LLMService, StructuredOutputError
from agentic_core.llm.service import extract_json_object
from tests.helpers import requirements_output

RAW = {"status": "ready", "count": 3, "tags": ["a", "b"]}


def test_extract_exact_json():
    assert extract_json_object(json.dumps(RAW)) == RAW


def test_extract_from_fenced_block():
    assert extract_json_object(f"```json\n{json.dumps(RAW)}\n```") == RAW


def test_extract_surrounded_by_prose():
    text = f"Here is the result:\n{json.dumps(RAW)}\nHope that helps!"
    assert extract_json_object(text) == RAW


def test_extract_with_leading_prose():
    text = f'As requested, the payload is {json.dumps(RAW)}.'
    assert extract_json_object(text) == RAW


def test_extract_picks_outermost_of_multiple_objects():
    text = f"{{'ignored': 1}}\n{json.dumps(RAW)}\n{{'also': 2}}"
    assert extract_json_object(text) == RAW


def test_extract_recovers_trailing_comma():
    broken = '{"status": "ready", "count": 3, "tags": ["a", "b",],}'
    assert extract_json_object(broken) == RAW


def test_extract_trailing_comma_inside_string_untouched():
    text = '{"message": "a, b, c]", "ok": true,}'
    assert extract_json_object(text) == {"message": "a, b, c]", "ok": True}


def test_extract_empty_raises_with_clear_error():
    with pytest.raises(StructuredOutputError) as exc:
        extract_json_object("   ")
    assert "empty" in str(exc.value)


def test_extract_garbage_raises_with_preview():
    with pytest.raises(StructuredOutputError) as exc:
        extract_json_object("sorry, no json here, just prose")
    assert "Raw text preview" in str(exc.value)


async def test_repair_skipped_after_slow_generation(provider, llm_service, make_context, settings):
    async def slow(_system, _user):
        await asyncio.sleep(0.05)
        return "this is not json"

    provider.set_handler(slow)
    settings.llm_poll_timeout_s = 0.01  # repair-skip threshold = 0.008s
    service = LLMService(provider, settings)
    agent = RequirementsAgent(service)

    result = await agent.run(make_context("Food delivery."))

    assert result.status == "failed"
    assert "repair was skipped" in (result.error or "")
    assert len(provider.calls) == 1  # no expensive repair round-trip


async def test_repair_runs_when_generation_is_fast(provider, llm_service, make_context, settings):
    responses = ["this is not json", json.dumps(requirements_output())]
    provider.set_responses(responses)
    settings.llm_poll_timeout_s = 60.0  # generous threshold, repair should run
    service = LLMService(provider, settings)
    agent = RequirementsAgent(service)

    result = await agent.run(make_context("Food delivery."))

    assert result.status == "success"
    assert result.retry_count == 1
    assert len(provider.calls) == 2


async def test_repair_prompt_truncates_huge_previous_response(provider, llm_service, make_context, settings):
    """A giant (possibly rambling) previous response is capped in the repair
    prompt so a repair does not re-send hundreds of KB of input tokens."""
    huge = "not json " * 800  # ~7200 chars
    provider.set_responses([huge, json.dumps(requirements_output())])
    settings.llm_poll_timeout_s = 60.0
    service = LLMService(provider, settings)
    agent = RequirementsAgent(service)

    result = await agent.run(make_context("Food delivery."))

    assert result.status == "success"
    repair_prompt = provider.calls[1][1]
    assert "chars omitted" in repair_prompt
    assert len(repair_prompt) < len(provider.calls[0][1]) + 5000