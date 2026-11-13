"""Immutable per-turn configuration snapshot.

A session stores only catalog ids (``model`` and ``harness``).  When a
turn starts, those ids are resolved into one :class:`TurnSnapshot` that
carries the provider, the harness spec, the tool registry and the runtime
config for that turn alone.  The turn then uses the snapshot for its whole
lifetime, so changing a session mid-run cannot affect the turn that is
already executing: the change applies to the next one.
"""

from __future__ import annotations

from dataclasses import dataclass, replace

from agent_runtime.agent.chat import ChatModel
from agent_runtime.agent.turn_factory import SummarizerFactory
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
    """Build the compaction model with this turn's endpoint and key.

    The providers-layer default only passes ``model`` and re-reads the
    environment for the endpoint, which would send a per-model base URL
    or key back to the process default.  Binding the whole runtime keeps
    the summarizer on the same endpoint as the turn.
    """

    def _build() -> ChatModel:
        model = runtime.summary_model or runtime.model
        return OpenAICompatibleLLM(config=replace(runtime, model=model))

    return _build


def resolve_turn(*, catalog: ModelCatalog, model_id: str | None,
                 harness_id: str | None,
                 base_runtime: RuntimeConfig) -> TurnSnapshot:
    """Freeze the catalog entry and harness into one turn snapshot.

    Raises :class:`~agent_runtime.model_catalog.ModelCatalogError` for an
    unknown model and :class:`~agent_runtime.harness.HarnessError` for an
    unknown harness, so the HTTP layer can report a useful 4xx instead of
    failing in the middle of a run.
    """
    entry: ModelEntry = catalog.require(model_id)
    runtime = replace(base_runtime, model=entry.model_id,
                      base_url=entry.base_url, api_key=entry.api_key)
    harness = resolve_harness(harness_id)
    return TurnSnapshot(
        model=entry.id,
        runtime=runtime,
        harness=harness,
        llm=OpenAICompatibleLLM(config=runtime),
        tools=create_default_registry(enabled=list(harness.tools.enabled)),
        stream=runtime.streaming,
        summarizer_factory=_summarizer_factory(runtime),
    )


__all__ = ["TurnSnapshot", "resolve_turn"]
