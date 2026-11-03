"""Composition root for one agent turn.

The factory resolves defaults, binds the observation spiller to the same
workspace as the file tools, builds the per-turn finisher, and hands the
completed :class:`~agent_runtime.agent.turn_collaborators.TurnCollaborators`
to :class:`~agent_runtime.agent.loop.AgentTurn`.  The turn itself
therefore never has to inspect incidental attributes of the tool layer
or construct its own policy helpers.
"""

from __future__ import annotations

from functools import lru_cache
from pathlib import Path

from agent_runtime.agent.chat import ChatModel
from agent_runtime.agent.loop import AgentTurn
from agent_runtime.agent.observations import ObservationFormatter
from agent_runtime.agent.tool_runner import ToolRunner
from agent_runtime.agent.turn_collaborators import TurnCollaborators
from agent_runtime.agent.turn_finisher import TurnFinisher
from agent_runtime.agent.turn_guard import TurnGuard
from agent_runtime.agent.turn_history import Conversation
from agent_runtime.agent.verification import SubmissionRules
from agent_runtime.events import EventEmitter
from agent_runtime.execution.base import Workspace
from agent_runtime.harness import HarnessSpec, default_harness
from agent_runtime.runtime_config import RuntimeConfig
from agent_runtime.tools import ToolExecutor
from agent_runtime.trace import RunTrace


@lru_cache(maxsize=32)
def _cached_summarizer(summary_model: str | None) -> ChatModel:
    """Per-model cached summarizer provider."""
    from agent_runtime.providers.llm import OpenAICompatibleLLM

    return OpenAICompatibleLLM(model=summary_model)


def _default_summarizer(runtime: RuntimeConfig | None = None) -> ChatModel:
    """Build the per-process summarizer provider on first use.

    Lazy on purpose: turns whose context strategy never summarizes must
    not construct an OpenAI client (or require an API key).
    Resolved from the unified runtime config so an explicit
    ``summary_model`` never falls back to a stale env read.
    """
    model = (runtime.summary_model if runtime is not None
             else RuntimeConfig.from_env().summary_model)
    return _cached_summarizer(model)


def create_agent_turn(
    user_message: str,
    llm: ChatModel,
    tools: ToolExecutor,
    max_iterations: int | None = None,
    *,
    harness: HarnessSpec | None = None,
    conversation: Conversation | None = None,
    stream: bool = False,
    trace: RunTrace | None = None,
    events: EventEmitter | None = None,
    skills_dir: str | Path | None = None,
    workspace: Workspace | None = None,
    runtime: RuntimeConfig | None = None,
) -> AgentTurn:
    """Assemble one :class:`AgentTurn`; the caller decides when to run it.

    ``workspace`` is the single file-storage instance used by both the
    tool layer and observation spilling.  When omitted, the executor's
    own workspace is used; every ``ToolExecutor`` guarantees one, so the
    formatter always has a real archive target.
    """
    resolved_runtime = runtime if runtime is not None else RuntimeConfig.from_env()
    resolved_harness = harness if harness is not None else default_harness()
    resolved_trace = (trace if trace is not None
                      else RunTrace(harness=resolved_harness))
    resolved_events = (events if events is not None
                       else EventEmitter(run_id=resolved_trace.run_id))
    resolved_conversation = (conversation if conversation is not None
                             else Conversation())
    resolved_workspace = (workspace if workspace is not None
                          else tools.workspace)
    resolved_max_iterations = (
        max_iterations if max_iterations is not None
        else resolved_harness.control.max_iterations)
    observations = ObservationFormatter(
        resolved_workspace,
        max_chars=resolved_harness.control.max_observation_chars,
        spill_preview_chars=resolved_harness.control.spill_preview_chars,
        archive_min_chars=resolved_harness.context.head_chars)
    # One finisher and one guard per turn: both carry turn-local state
    # (finish violations, same-tool error counts).
    rules = SubmissionRules.from_genome(resolved_harness.verification)
    finisher = TurnFinisher(
        rules=rules,
        tools=tools,
        trace=resolved_trace,
        events=resolved_events,
        max_iterations=resolved_max_iterations,
        finish_violation_limit=resolved_harness.control.finish_violation_limit)
    guard = TurnGuard(
        harness=resolved_harness,
        rules=rules,
        trace=resolved_trace,
        events=resolved_events,
        max_iterations=resolved_max_iterations)
    tool_runner = ToolRunner(
        tools=tools,
        harness=resolved_harness,
        observations=observations,
        finisher=finisher,
        guard=guard,
        trace=resolved_trace,
        events=resolved_events)

    return AgentTurn(user_message, TurnCollaborators(
        llm=llm,
        tools=tools,
        workspace=resolved_workspace,
        harness=resolved_harness,
        conversation=resolved_conversation,
        trace=resolved_trace,
        events=resolved_events,
        observations=observations,
        finisher=finisher,
        guard=guard,
        tool_runner=tool_runner,
        summarizer=lambda: _default_summarizer(resolved_runtime),
        stream=stream,
        max_iterations=resolved_max_iterations,
        skills_dir=skills_dir,
        runtime=resolved_runtime,
    ))


async def run_turn(
    user_message: str,
    llm: ChatModel,
    tools: ToolExecutor,
    max_iterations: int | None = None,
    *,
    harness: HarnessSpec | None = None,
    conversation: Conversation | None = None,
    stream: bool = False,
    trace: RunTrace | None = None,
    events: EventEmitter | None = None,
    skills_dir: str | Path | None = None,
    workspace: Workspace | None = None,
    runtime: RuntimeConfig | None = None,
) -> str:
    """Create and run one agent turn under the given harness.

    ``max_iterations`` explicitly overrides ``harness.control``; the
    harness is otherwise the source of truth for how the agent behaves.
    """
    turn = create_agent_turn(
        user_message,
        llm,
        tools,
        max_iterations,
        harness=harness,
        conversation=conversation,
        stream=stream,
        trace=trace,
        events=events,
        skills_dir=skills_dir,
        workspace=workspace,
        runtime=runtime,
    )
    return await turn.run()


__all__ = ["create_agent_turn", "run_turn"]
