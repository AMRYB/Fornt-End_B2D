"""Shared fixtures: hermetic settings, fake LLM provider, services."""

from __future__ import annotations

import pytest

from agentic_core.artifacts import ArtifactStore
from agentic_core.config import Settings
from agentic_core.llm import FakeLLMProvider, LLMService
from agentic_core.orchestrator import EventBus, ExecutionTracker, Orchestrator
from agentic_core.schemas import ProjectContext


@pytest.fixture
def settings(tmp_path):
    return Settings(cursor_api_key="test-key", data_dir=tmp_path)


@pytest.fixture
def provider():
    return FakeLLMProvider()


@pytest.fixture
def llm_service(provider, settings):
    return LLMService(provider, settings)


@pytest.fixture
def event_bus():
    return EventBus()


@pytest.fixture
def tracker(settings):
    return ExecutionTracker(settings.runs_dir)


@pytest.fixture
def artifact_store(settings):
    return ArtifactStore(settings.artifacts_dir)


@pytest.fixture
def make_orchestrator(provider, settings, event_bus, tracker):
    def _make(**overrides):
        current = settings.model_copy(update=overrides)
        service = LLMService(provider, current)
        return Orchestrator(service, event_bus, tracker, current)

    return _make


@pytest.fixture
def make_context():
    def _make(idea: str, project_id: str = "test_proj") -> ProjectContext:
        return ProjectContext(project_id=project_id, business_idea=idea)

    return _make