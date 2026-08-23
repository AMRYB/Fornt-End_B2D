"""Execution events and a simple in-process event bus.

The bus buffers recent events per project so late-connecting SSE consumers
still see progress, and delivers live events to active subscribers.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import datetime
from typing import AsyncIterator


@dataclass
class AgentEvent:
    event: str
    project_id: str
    agent: str | None = None
    status: str | None = None
    duration_ms: int | None = None
    reason: str | None = None
    message: str | None = None
    input_chars: int | None = None
    output_chars: int | None = None
    # Which call of the agent this is (1 = first; >1 = regenerated/retried).
    invocation: int | None = None
    timestamp: datetime = field(default_factory=datetime.now)

    def to_dict(self) -> dict:
        data = {
            "event": self.event,
            "project_id": self.project_id,
            "timestamp": self.timestamp.isoformat(),
        }
        for key in (
            "agent", "status", "duration_ms", "reason", "message",
            "input_chars", "output_chars", "invocation",
        ):
            value = getattr(self, key)
            if value is not None:
                data[key] = value
        return data


class EventBus:
    def __init__(self, buffer_size: int = 500):
        self._buffers: dict[str, list[AgentEvent]] = {}
        self._queues: dict[str, list[asyncio.Queue]] = {}
        self._listeners: list[object] = []
        self._buffer_size = buffer_size

    def subscribe(self, listener) -> None:
        """Register a synchronous callable invoked for every emitted event."""
        if listener not in self._listeners:
            self._listeners.append(listener)

    def unsubscribe(self, listener) -> None:
        if listener in self._listeners:
            self._listeners.remove(listener)

    def emit(self, event: AgentEvent) -> None:
        buffer = self._buffers.setdefault(event.project_id, [])
        buffer.append(event)
        if len(buffer) > self._buffer_size:
            buffer.pop(0)
        for queue in self._queues.get(event.project_id, []):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                pass
        for listener in self._listeners:
            listener(event)

    async def stream(self, project_id: str) -> AsyncIterator[AgentEvent]:
        """Yield buffered events then live events for a project."""
        queue: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._queues.setdefault(project_id, []).append(queue)
        try:
            for event in self._buffers.get(project_id, []):
                yield event
            while True:
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=15.0)
                    yield event
                except asyncio.TimeoutError:
                    yield AgentEvent(event="heartbeat", project_id=project_id)
        finally:
            try:
                self._queues[project_id].remove(queue)
            except ValueError:
                pass