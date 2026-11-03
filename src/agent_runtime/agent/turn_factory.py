"""Composition root for one agent turn.

The factory resolves defaults, binds the observation spiller to the same
workspace as the file tools, builds the per-turn finisher, and hands the
completed :class:`~agent_runtime.agent.turn_collaborators.TurnCollaborators`
to :class:`~agent_runtime.agent.loop.AgentTurn`.  The turn itself
therefore never has to inspect incidental attributes of the tool layer
or construct its own policy helpers.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Any

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

# Summarizer provider factory type: zero-arg callable returning a ChatModel.
# The default comes from ``agent_runtime.providers.factory`` (lazy import
# so ``agent`` never names a concrete provider class at module scope).
SummarizerFactory = Callable[[], ChatModel]


def _default_summarizer_factory(runtime: RuntimeConfig) -> SummarizerFactory:
    """Default lazy summarizer factory bound to ``runtime``."""
    def _build() -> ChatModel:
        from agent_runtime.providers.factory import default_summarizer

        return default_summarizer(runtime)
    return _build


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
    summarizer_factory: SummarizerFactory | None = None,
) -> AgentTurn:
    """Assemble one :class:`AgentTurn`; the caller decides when to run it.

    ``workspace`` is the single file-storage instance used by both the
    tool layer and observation spilling.  When omitted, the executor's
    own workspace is used; every ``ToolExecutor`` guarantees one, so the
    formatter always has a real archive target.

    ``summarizer_factory`` builds the context-compaction summarizer LLM
    on first use.  When omitted, the providers-layer default (resolved
    from ``runtime.summary_model``) applies — still lazy, so turns whose
    strategy never summarizes never construct a provider client.
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
    resolved_factory = (summarizer_factory if summarizer_factory is not None
                        else _default_summarizer_factory(resolved_runtime))

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
        summarizer=resolved_factory,
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
    summarizer_factory: SummarizerFactory | None = None,
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
        summarizer_factory=summarizer_factory,
    )
    return await turn.run()


__all__ = ["create_agent_turn", "run_turn", "SummarizerFactory"]
