"""Immutable per-turn configuration snapshot.

A session stores only catalog ids (``model`` and ``harness``).  When a
turn starts, those ids are resolved into one :class:`TurnSnapshot` that
carries the provider, harness spec, tool registry and runtime config for
that turn alone. The turn uses the snapshot for its whole lifetime, so a
session change mid-run cannot affect the turn already executing.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from agent_runtime.agent.llm_request import ChatModel
from agent_runtime.agent.turn_factory import SummarizerFactory
from agent_runtime.diagnosis import EVALUATION_HARNESS_ID, diagnosis_tool_specs
from agent_runtime.harness import HarnessSpec, resolve_harness
from agent_runtime.model_catalog import ModelCatalog, ModelEntry
from agent_runtime.providers import OpenAICompatibleLLM
from agent_runtime.runtime_config import RuntimeConfig
from agent_runtime.tools import ToolExecutor, create_default_registry


@dataclass(frozen=True)
class TurnSnapshot:
    """Everything one turn needs, frozen at the moment it starts."""

    model: str
    runtime: RuntimeConfig
    harness: HarnessSpec
    llm: ChatModel
    tools: ToolExecutor
    stream: bool
    summarizer_factory: SummarizerFactory


def _summarizer_factory(runtime: RuntimeConfig) -> SummarizerFactory:
    """Build the compaction model with this turn's endpoint and key."""

    def _build() -> ChatModel:
        model = runtime.summary_model or runtime.model
        # Summarizing runs with ``tools=None`` and reasoning effort "none":
        # nothing is replayed, so pin the irrelevant bit off.
        return OpenAICompatibleLLM(config=replace(runtime, model=model),
                                   echo_reasoning=False)

    return _build


def _turn_tools(harness: HarnessSpec, runtime: RuntimeConfig) -> ToolExecutor:
    """Tools of one harness, plus the read-only diagnosis tools.

    The extra specs are named in ``harnesses/evaluation-v1.yaml``; a
    generic harness never advertises ``read_trace`` / ``search_trace`` /
    ``read_diagnosis``, so a task agent cannot read the recorded evidence.
    """
    extra = (diagnosis_tool_specs()
             if harness.id == EVALUATION_HARNESS_ID else None)
    return create_default_registry(enabled=list(harness.tools.enabled),
                                   extra_specs=extra, runtime=runtime)


def resolve_turn(*, catalog: ModelCatalog, model_id: str | None,
                 harness_id: str | None,
                 base_runtime: RuntimeConfig) -> TurnSnapshot:
    """Freeze the catalog entry and harness into one turn snapshot."""
    entry: ModelEntry = catalog.require(model_id)
    runtime = replace(base_runtime, model=entry.model_id,
                      base_url=entry.base_url, api_key=entry.api_key)
    harness = resolve_harness(harness_id)
    return TurnSnapshot(
        model=entry.id,
        runtime=runtime,
        harness=harness,
        llm=OpenAICompatibleLLM(config=runtime,
                                echo_reasoning=entry.echo_reasoning),
        tools=_turn_tools(harness, runtime),
        stream=runtime.streaming,
        summarizer_factory=_summarizer_factory(runtime),
    )


__all__ = ["TurnSnapshot", "resolve_turn"]
