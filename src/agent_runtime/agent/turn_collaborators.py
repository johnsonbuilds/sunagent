"""The collaborators one agent turn is assembled from.

The bundle carries already-built objects into
:class:`~agent_runtime.agent.loop.AgentTurn`; it holds no turn policy
itself.  Some collaborators are stateless services (the LLM, the event
emitter), while others are per-turn stateful helpers (the observation
formatter, the finisher) — the factory creates one of each per turn.
Keeping the bundle separate from both the turn and its factory avoids a
circular import and makes the wiring explicit — especially
``workspace``, which the file tools and the observation spiller must
share.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_runtime.agent.chat import ChatModel
    from agent_runtime.agent.observations import ObservationFormatter
    from agent_runtime.agent.turn_finisher import TurnFinisher
    from agent_runtime.agent.turn_history import Conversation
    from agent_runtime.events import EventEmitter
    from agent_runtime.execution.base import Workspace
    from agent_runtime.harness import HarnessSpec
    from agent_runtime.tools import ToolExecutor
    from agent_runtime.trace import RunTrace


@dataclass(frozen=True)
class TurnCollaborators:
    """Every object an :class:`AgentTurn` collaborates with for one turn."""

    llm: ChatModel
    tools: ToolExecutor
    workspace: Workspace
    harness: HarnessSpec
    conversation: Conversation
    trace: RunTrace
    events: EventEmitter
    observations: ObservationFormatter
    finisher: TurnFinisher
    stream: bool
    max_iterations: int
    skills_dir: str | Path | None


__all__ = ["TurnCollaborators"]
