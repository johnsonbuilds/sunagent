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
  compaction summary was PRODUCED. Only a genuinely new snapshot moves it
  (no-op refreshes and snapshot reuses keep it): the incremental
  summarizer reads only ``messages[boundary:]`` plus the previous
  summary, so a second compaction costs O(new segment), not O(full
  history). Advancing it on every refresh would chase ``len(messages)``
  and starve later compactions of input, freezing the snapshot.
* ``_usage_anchor`` — (measured prompt tokens, view length) from the
  last response that reported billed usage. Token-budget estimates
  extrapolate from this anchor: measured truth below it, per-message
  char-ratio estimates above it.
"""

from __future__ import annotations

from typing import Any

from agent_runtime.agent.context import (
    _extract_summary,
    _reasoning_replayed,
    _total_chars,
    apply_context_strategy,
)
from agent_runtime.harness import ContextGenome


def _ensure_system_prompt(conversation: Conversation, system: str,
                         skills_block: str = "",
                         memory_block: str = "") -> bool:
    """Insert the harness system prompt once, before the first user message.

    Returns True when a system message was inserted, False when the
    conversation already had one (or there was nothing to insert).
    Callers use the return value to decide whether injection-point-A
    trace events (``skills.loaded``, ``memory.recall``) describe a
    prompt the model will actually see.

    ``memory_block`` (persistent-memory convention plus tree) comes
    before ``skills_block`` (rendered plain skill text) after ``system``
    as part of the same system message (injection point A): memory
    carries session-level identity (who the user is, what persists),
    skills carry task-level procedure (how to act) — the more stable
    context leads.  One resident system message keeps memory
    strategies coherent.
    """
    parts = [part for part in (system, memory_block, skills_block) if part]
    if not parts:
        return False
    if any(message.get("role") == "system" for message in conversation.messages):
        return False
    conversation.messages.insert(
        0, {"role": "system", "content": "\n\n".join(parts)})
    return True


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
        Reasoning chars are counted exactly as the budget layer counts
        them (echo policy); otherwise the anchor ratio would skew the
        moment the endpoint stops replaying reasoning.
        """
        if prompt_tokens > 0:
            self._usage_anchor = (prompt_tokens, len(self.view),
                                  _total_chars(self.view, _reasoning_replayed()))

    @property
    def usage_anchor(self) -> tuple[int, int, int] | None:
        return self._usage_anchor

    async def refresh_view(self, context: ContextGenome, *, trace: Any = None,
                           iteration: int | None = None,
                           force_budget: int | None = None) -> None:
        """Rebuild the request view via the context genome's strategy.

        ``force_budget`` (used by the context-window hard gate) shrinks
        the working budget so the strategy must compact below the size
        that the provider just rejected.
        """
        new_view = await apply_context_strategy(
            self.messages, context, compact_messages=self.view,
            trace=trace, iteration=iteration,
            boundary=self._summary_boundary,
            usage_anchor=self._usage_anchor,
            force_budget=force_budget)
        if new_view is not self.messages:
            if new_view is not self.view:
                # The view was rebuilt: the anchor measured the OLD mix
                # (tool-heavy), not the new summary-heavy mix. Drop it so
                # the next estimate uses the default ratio until the next
                # billed response re-anchors (one iteration later) —
                # a stale ratio drifts worse than no ratio.
                self._usage_anchor = None
            old_summary = _extract_summary(self.view)
            self.view = new_view
            new_summary = _extract_summary(new_view)
            # Advance past the summarized region only when a NEW snapshot
            # was produced. No-op refreshes (view already under budget) and
            # snapshot reuses (nothing novel to summarise) keep the old text:
            # advancing there would chase len(messages) every iteration,
            # leaving later compactions an empty segment and freezing the
            # snapshot forever while history grows past it.
            if new_summary is not None and new_summary != old_summary:
                self._summary_boundary = len(self.messages)


__all__ = ["Conversation", "TurnHistory", "_ensure_system_prompt"]
