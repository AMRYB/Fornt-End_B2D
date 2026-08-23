"""Shared application services for the FastAPI layer."""

from __future__ import annotations

import asyncio

from ..artifacts import ArtifactStore
from ..config import get_settings
from ..llm import aclose_llm_services, create_agent_llm_services
from ..orchestrator import EventBus, ExecutionTracker, Orchestrator
from ..project_store import ProjectStore
from ..supabase_persistence import (
    SupabaseArtifactStore,
    SupabaseExecutionTracker,
    SupabaseGateway,
    SupabaseProjectStore,
    SupabaseWorkflowStore,
)


class AppServices:
    def __init__(self):
        self.settings = get_settings()
        self.event_bus = EventBus()
        self.gateway: SupabaseGateway | None = None
        self.workflow_store: SupabaseWorkflowStore | None = None
        if self.settings.supabase_configured:
            self.gateway = SupabaseGateway(
                self.settings.supabase_url,
                self.settings.supabase_anon_key,
                self.settings.supabase_service_role_key,
            )
            self.tracker = SupabaseExecutionTracker(self.gateway)
            self.project_store = SupabaseProjectStore(self.gateway)
            self.artifact_store = SupabaseArtifactStore(self.gateway)
            self.workflow_store = SupabaseWorkflowStore(self.gateway)
        else:
            self.tracker = ExecutionTracker(self.settings.runs_dir)
            self.project_store = ProjectStore(
                self.settings.db_path, legacy_dir=self.settings.projects_dir
            )
            self.artifact_store = ArtifactStore(self.settings.artifacts_dir)
        self.llm_services = create_agent_llm_services(self.settings)
        self.orchestrator = Orchestrator(
            self.llm_services, self.event_bus, self.tracker, self.settings
        )

    async def aclose(self) -> None:
        """Close every distinct provider exactly once."""
        await aclose_llm_services(self.llm_services)
        if self.gateway is not None:
            await asyncio.to_thread(self.gateway.close)


services = AppServices()
