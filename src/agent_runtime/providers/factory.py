"""Summarizer provider factory (providers-layer ownership).

The agent composition root needs a summarizer LLM only when the active
context strategy summarizes. Building it here — not in ``agent/`` —
keeps the agent layer from naming a concrete provider class
(``OpenAICompatibleLLM``), satisfying DIP: ``agent`` depends on the
``ChatModel`` protocol plus this factory function, and swapping the
provider only touches ``providers/``.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any


@lru_cache(maxsize=32)
def _cached_summarizer(summary_model: str | None) -> Any:
    """Per-model cached summarizer provider."""
    from agent_runtime.providers.llm import OpenAICompatibleLLM

    return OpenAICompatibleLLM(model=summary_model)


def default_summarizer(runtime: Any | None = None) -> Any:
    """Build the per-process summarizer provider on first use.

    Lazy on purpose: turns whose context strategy never summarizes must
    not construct an OpenAI client (or require an API key). Resolved
    from the unified runtime config so an explicit ``summary_model``
    never falls back to a stale env read.
    """
    if runtime is not None and getattr(runtime, "summary_model", None) is not None:
        model = runtime.summary_model
    else:
        from agent_runtime.runtime_config import RuntimeConfig

        model = (runtime.summary_model if runtime is not None
                 else RuntimeConfig.from_env().summary_model)
    return _cached_summarizer(model)


__all__ = ["default_summarizer"]
