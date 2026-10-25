"""The turn-local conversation: full history plus its request view.

``messages`` is the single source of truth shared across turns; ``view``
is the (possibly compacted) projection actually sent to the model and
watched by the budget gate. The two are appended in lockstep through a
single entry point, so the invariant that used to rest on discipline at
every append site — update one list, never forget the other — is now
enforced by construction.
"""

from __future__ import annotations

from typing import Any

from agent_runtime.agent.memory import apply_memory_strategy
from agent_runtime.harness import MemoryGenome


class Conversation:
    """Conversation state shared by multiple agent turns."""

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    def append(self, message: dict[str, Any]) -> None:
        self.messages.append(message)


def _ensure_system_prompt(conversation: Conversation, system: str,
                         skills_block: str = "") -> None:
    """Insert the harness system prompt once, before the first user message.

    ``skills_block`` (rendered plain skill text) is appended after
    ``system`` as part of the same system message (injection point A),
    so memory strategies see one resident system message.
    """
    parts = [part for part in (system, skills_block) if part]
    if not parts:
        return
    if any(message.get("role") == "system" for message in conversation.messages):
        return
    conversation.messages.insert(
        0, {"role": "system", "content": "\n\n".join(parts)})


class TurnHistory:
    """Conversation history and its request view, appended in lockstep.

    ``view`` starts as an independent copy of the initialized
    conversation so the two never alias. ``refresh_view`` may replace
    the view wholesale when a memory strategy produces a compacted copy;
    subsequent appends still land in both. When the strategy returns
    ``messages`` itself (``full_history``), the view keeps its own
    identity — it must never alias the full list, or mirrored appends
    would double up.
    """

    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self.messages = messages
        self.view: list[dict[str, Any]] = list(messages)

    def append(self, message: dict[str, Any]) -> None:
        """Append one message to the history and the request view."""
        self.messages.append(message)
        self.view.append(message)

    def append_to_view(self, message: dict[str, Any]) -> None:
        """Append a transient notice to the request view only.

        Mirror receipts and similar meta-notices must not pollute
        ``messages`` (the audit/verification record); they ride the next
        LLM request and are dropped on the next view replacement.
        """
        self.view.append(message)

    def replace_view(self, new_view: list[dict[str, Any]],
                     protect: int = 0) -> None:
        """Swap the request view after a mirror sync (L2 self-managed ctx).

        ``messages`` is never touched.  The protected prefix must still
        match, so a corrupted mirror cannot rewrite the system/task prompt.
        """
        safe = max(0, min(int(protect), len(self.messages), len(new_view)))
        if new_view[:safe] != self.messages[:safe]:
            raise ValueError("mirror sync refused: protected prefix changed")
        self.view = new_view

    async def refresh_view(self, memory: MemoryGenome, *, trace: Any = None,
                           iteration: int | None = None) -> None:
        """Rebuild the request view via the memory genome's strategy."""
        new_view = await apply_memory_strategy(
            self.messages, memory, compact_messages=self.view,
            trace=trace, iteration=iteration)
        if new_view is not self.messages:
            self.view = new_view


__all__ = ["Conversation", "TurnHistory"]
