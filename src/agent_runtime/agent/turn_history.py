"""The turn-local conversation: full history plus its request view.

``messages`` is the single source of truth shared across turns; ``view``
is the (possibly compacted) projection actually sent to the model and
watched by the budget gate. The two are appended in lockstep through a
single entry point, so the invariant that used to rest on discipline at
every append site — update one list, never forget the other — is now
enforced by construction.

Two cross-compaction anchors live here as projection metadata (they
never travel on the wire):

* ``_summary_boundary`` — the ``messages`` length when the latest
  compaction summary was produced. The incremental summarizer reads
  only ``messages[boundary:]`` plus the previous summary, so a second
  compaction costs O(new segment), not O(full history).
* ``_usage_anchor`` — (measured prompt tokens, view length) from the
  last response that reported billed usage. Token-budget estimates
  extrapolate from this anchor: measured truth below it, per-message
  char-ratio estimates above it.
"""

from __future__ import annotations

from typing import Any

from agent_runtime.agent.memory import SUMMARY_PREFIX, _total_chars, apply_memory_strategy
from agent_runtime.harness import MemoryGenome


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


class Conversation:
    """Conversation state shared by multiple agent turns."""

    def __init__(self) -> None:
        self.messages: list[dict[str, Any]] = []

    def append(self, message: dict[str, Any]) -> None:
        self.messages.append(message)


class TurnHistory:
    """Full history (source of truth) plus its request-view projection."""

    def __init__(self, messages: list[dict[str, Any]]) -> None:
        self.messages = messages
        self.view: list[dict[str, Any]] = list(messages)
        # messages length at the last summary-producing compaction.
        self._summary_boundary: int = 0
        # (prompt_tokens, view length, view chars) of the last measured
        # response: the token estimator's calibration anchor.
        self._usage_anchor: tuple[int, int, int] | None = None

    def append(self, message: dict[str, Any]) -> None:
        """Append one message to the history and the request view."""
        self.messages.append(message)
        self.view.append(message)

    @property
    def summary_boundary(self) -> int:
        """``messages`` index one past the last summarized message."""
        return self._summary_boundary

    def record_usage(self, prompt_tokens: int) -> None:
        """Anchor the token estimate at the provider's billed usage.

        ``prompt_tokens`` is the measured size of the request the
        provider just accepted. Anchoring at the current view length
        lets the estimator extrapolate only over messages appended
        after the measurement — the short, well-calibrated tail.
        """
        if prompt_tokens > 0:
            self._usage_anchor = (prompt_tokens, len(self.view),
                                  _total_chars(self.view, True))

    @property
    def usage_anchor(self) -> tuple[int, int, int] | None:
        return self._usage_anchor

    async def refresh_view(self, memory: MemoryGenome, *, trace: Any = None,
                           iteration: int | None = None,
                           force_budget: int | None = None) -> None:
        """Rebuild the request view via the memory genome's strategy.

        ``force_budget`` (used by the context-window hard gate) shrinks
        the working budget so the strategy must compact below the size
        that the provider just rejected.
        """
        new_view = await apply_memory_strategy(
            self.messages, memory, compact_messages=self.view,
            trace=trace, iteration=iteration,
            boundary=self._summary_boundary,
            usage_anchor=self._usage_anchor,
            force_budget=force_budget)
        if new_view is not self.messages:
            self.view = new_view
            if any(message.get("role") == "user"
                   and str(message.get("content", "")).startswith(SUMMARY_PREFIX)
                   for message in self.view):
                self._summary_boundary = len(self.messages)


__all__ = ["Conversation", "TurnHistory", "_ensure_system_prompt"]
