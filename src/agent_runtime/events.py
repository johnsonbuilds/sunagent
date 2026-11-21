"""User-facing agent events, decoupled from internal trace events.

The runtime emits two related but distinct streams:

* Internal trace events (:class:`agent_runtime.trace.RunTrace`) for
  debugging, observability, and failure analysis. They stay compact and
  provider-independent, and never contain model text.
* User-facing agent events (:class:`EventEmitter`) for channels such as
  the CLI, a web UI, or chat adapters. They carry what a user needs to
  watch the run: streamed assistant text, tool arguments, and result
  summaries.

Channels only convert agent events into their own presentation; the
runtime never formats output for a specific frontend.

Event vocabulary (``call_id`` is a top-level field, not part of ``data``):

    agent.started        {message}
    agent.completed      {iterations, answer}
    assistant.started    {}
    assistant.delta      {content, reasoning}
    assistant.completed  {tool_calls}
    tool.started         {tool, arguments}
    tool.completed       {duration} plus either a shell summary
                         {exit_code, stdout_tail, stderr_tail, error?}
                         or a generic {result}
    tool.failed          {tool, error}
    runtime.error        {stage, error}
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable
from dataclasses import asdict, dataclass
from typing import Any
from uuid import uuid4


logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgentEvent:
    """One immutable user-facing event emitted by an agent run."""

    run_id: str
    event_id: str
    event_type: str
    timestamp: float
    iteration: int
    call_id: str | None
    data: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


_STREAM_CLOSED: Any = object()

# Streamed token deltas are consumed live (subscribers / SSE); keeping
# them in ``events`` makes memory grow with output length for no gain.
_UNRETAINED = frozenset({"assistant.delta"})


class EventEmitter:
    """Fan out agent events to synchronous and asynchronous consumers."""

    def __init__(self, run_id: str | None = None) -> None:
        self.run_id = run_id or f"run-{uuid4().hex}"
        self.events: list[AgentEvent] = []
        self._subscribers: list[Callable[[AgentEvent], None]] = []
        self._queues: list[asyncio.Queue[AgentEvent]] = []

    def subscribe(self, callback: Callable[[AgentEvent], None]) -> Callable[[], None]:
        """Register a synchronous callback and return an unsubscribe function."""
        self._subscribers.append(callback)

        def unsubscribe() -> None:
            try:
                self._subscribers.remove(callback)
            except ValueError:
                pass

        return unsubscribe

    def stream(self) -> AsyncIterator[AgentEvent]:
        """Consume events asynchronously until :meth:`close` is called.

        Events emitted between creating and iterating the stream are
        buffered, so the consumer never misses anything.
        """
        queue: asyncio.Queue[AgentEvent] = asyncio.Queue()
        self._queues.append(queue)
        return self._consume(queue)

    def emit(self, event_type: str, iteration: int = 0, *,
             call_id: str | None = None, **data: Any) -> AgentEvent:
        """Emit one event without ever blocking the agent run."""
        event = AgentEvent(self.run_id, f"evt-{uuid4().hex}", event_type,
                           time.time(), iteration, call_id, data)
        if event_type not in _UNRETAINED:
            self.events.append(event)
        for subscriber in list(self._subscribers):
            try:
                subscriber(event)
            except Exception:
                logger.exception("event subscriber failed: %s",
                                 type(subscriber).__name__)
        for queue in self._queues:
            queue.put_nowait(event)
        return event

    def close(self) -> None:
        """Release async consumers; no more events will be delivered."""
        for queue in self._queues:
            queue.put_nowait(_STREAM_CLOSED)

    async def _consume(self, queue: asyncio.Queue[AgentEvent]) -> AsyncIterator[AgentEvent]:
        try:
            while True:
                event = await queue.get()
                if event is _STREAM_CLOSED:
                    break
                yield event
        finally:
            try:
                self._queues.remove(queue)
            except ValueError:
                pass


__all__ = ["AgentEvent", "EventEmitter"]
