"""Turn opening: everything injected before the first LLM request.

A turn's first request only differs from later ones by the resident
system prompt: the harness persona, the persistent-memory convention and
the resolved skills all ride one ``system`` message.  Assembling that
message (and tracing whether the model will actually see it) is a
one-shot concern, so it lives here instead of on the loop.
"""

from __future__ import annotations

from pathlib import Path

from agent_runtime.agent.turn_history import Conversation, ensure_system_prompt
from agent_runtime.execution.base import Workspace
from agent_runtime.harness import HarnessSpec
from agent_runtime.memory import render_memory_block
from agent_runtime.skills import (
    render_skills_block,
    resolve_skills,
    skills_trace_meta,
)
from agent_runtime.trace import RunTrace


def open_turn(*, conversation: Conversation, harness: HarnessSpec,
              workspace: Workspace, skills_dir: str | Path | None,
              trace: RunTrace) -> None:
    """Inject the system prompt / skills / memory blocks when absent.

    ``ensure_system_prompt`` is idempotent: on a conversation that already
    has a system message it is a no-op, so the ``skills.loaded`` and
    ``memory.recall`` events are only emitted when the blocks were really
    inserted (and will therefore reach the model).
    """
    skills = resolve_skills(list(harness.skills), skills_dir=skills_dir,
                            harness=harness)
    skills_block = render_skills_block(skills)
    memory_view = render_memory_block(workspace)
    inserted = ensure_system_prompt(conversation, harness.prompt.system,
                                    skills_block, memory_view.block)
    if not inserted:
        return
    if skills:
        trace.emit("skills.loaded", 0, **skills_trace_meta(skills))
    if memory_view.block:
        trace.emit("memory.recall", 0, **memory_view.trace_meta())


__all__ = ["open_turn"]
