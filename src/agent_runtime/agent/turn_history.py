"""The turn-local conversation: full history plus its request view.

``messages`` is the single source of truth shared across turns; ``view`` is the
(possibly compacted) projection actually sent to the model. Both are appended
through one entry point, so they cannot drift apart.

Two cross-compaction anchors live here as projection metadata (never on the
wire): ``_summary_boundary`` (``messages`` length when the latest snapshot was
produced) and ``_usage_anchor`` (measured prompt tokens + view size from the
last billed response).
"""

from __future__ import annotations

import logging
from typing import Any

from agent_runtime.agent.context import (
    ViewBudget,
    extract_summary,
    total_chars,
    apply_context_strategy,
    request_view_size,
)
from agent_runtime.harness import ContextGenome

logger = logging.getLogger(__name__)


def ensure_system_prompt(conversation: Conversation, system: str,
                         skills_block: str = "",
                         memory_block: str = "") -> bool:
    """Insert the harness system prompt once, before the first user message.

    Returns True when a system message was inserted, so callers can tell
    whether injection-point-A trace events describe a prompt the model will
    actually see.  Memory precedes skills: session-level identity is more
    stable than task-level procedure.
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

    def __init__(self, messages: list[dict[str, Any]], *,
                 summarizer: Any = None,
                 echo_reasoning: bool = True) -> None:
        self.messages = messages
        self.view: list[dict[str, Any]] = list(messages)
        # Explicit summarizer (provider object or lazy factory); when omitted,
        # the context layer's context-local default applies.
        self.summarizer = summarizer
        # Resolved once by the composition root (loop/turn factory) from the
        # provider's own endpoint: the budget layer never re-derives it.
        self.echo_reasoning = echo_reasoning
        self._summary_boundary: int = 0
        self._usage_anchor: tuple[int, int, int] | None = None
        # Turn-level billed-token ledger.  Summarizer sub-calls feed only their
        # own counters: their request never travels on ``view``, so they must
        # not move the anchor.
        self._main_prompt_tokens: int = 0
        self._main_completion_tokens: int = 0
        self._main_cached_tokens: int = 0
        self._summarizer_prompt_tokens: int = 0
        self._summarizer_completion_tokens: int = 0

    def append(self, message: dict[str, Any]) -> None:
        """Append one message to the history and the request view."""
        self.messages.append(message)
        self.view.append(message)

    @property
    def summary_boundary(self) -> int:
        """``messages`` index one past the last summarized message."""
        return self._summary_boundary

    def _budget(self, context: ContextGenome,
                anchor: tuple[int, int, int] | None) -> ViewBudget:
        """The one valuation of this view: genome budget + echo policy."""
        return ViewBudget.for_context(context, anchor=anchor,
                                      echo_reasoning=self.echo_reasoning)

    def record_usage(self, prompt_tokens: int,
                     completion_tokens: int = 0,
                     cached_tokens: int = 0) -> None:
        """Anchor the token estimate at the provider's billed usage.

        Anchoring at the current view length lets the estimator extrapolate
        only over messages appended after the measurement.  Reasoning chars
        are counted with the same echo policy as the budget layer, or the
        ratio would skew the moment the endpoint stops replaying reasoning.
        """
        prompt_tokens = int(prompt_tokens or 0)
        completion_tokens = int(completion_tokens or 0)
        self._main_prompt_tokens += prompt_tokens
        self._main_completion_tokens += completion_tokens
        self._main_cached_tokens += int(cached_tokens or 0)
        if prompt_tokens > 0:
            self._usage_anchor = (prompt_tokens, len(self.view),
                                  total_chars(self.view, self.echo_reasoning))

    def record_summarizer_usage(self, prompt_tokens: int,
                                completion_tokens: int = 0) -> None:
        """Accumulate one summarizer sub-call into the turn ledger."""
        self._summarizer_prompt_tokens += int(prompt_tokens or 0)
        self._summarizer_completion_tokens += int(completion_tokens or 0)

    @property
    def token_totals(self) -> dict[str, int]:
        """Cumulative billed tokens for the turn (main + summarizer)."""
        prompt = self._main_prompt_tokens + self._summarizer_prompt_tokens
        completion = (self._main_completion_tokens
                      + self._summarizer_completion_tokens)
        return {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": prompt + completion,
            # A subset of prompt_tokens, reported not summed.
            "cached_tokens": self._main_cached_tokens,
            "summarizer_prompt_tokens": self._summarizer_prompt_tokens,
            "summarizer_completion_tokens": self._summarizer_completion_tokens,
        }

    @property
    def usage_anchor(self) -> tuple[int, int, int] | None:
        return self._usage_anchor

    async def refresh_view(self, context: ContextGenome, *, trace: Any = None,
                           iteration: int | None = None,
                           force_budget: int | None = None) -> None:
        """Rebuild the request view via the context genome's strategy.

        ``force_budget`` (used by the context-window hard gate) shrinks the
        working budget so the strategy must compact below the size the
        provider just rejected.
        """
        new_view = await apply_context_strategy(
            self.messages, context, compact_messages=self.view,
            trace=trace, iteration=iteration,
            boundary=self._summary_boundary,
            budget=self._budget(context, self._usage_anchor),
            force_budget=force_budget,
            summarizer=self.summarizer,
            usage_sink=self.record_summarizer_usage)
        if new_view is not self.messages:
            if new_view != self.view:
                # The anchor measured the old tool-heavy mix, not the new
                # summary-heavy one; a stale ratio drifts worse than none.
                self._usage_anchor = None
            old_summary = extract_summary(self.view)
            self.view = new_view
            new_summary = extract_summary(new_view)
            # Advance only on a genuinely new snapshot: advancing on no-op
            # refreshes would chase ``len(messages)``, leaving later
            # compactions an empty segment and freezing the snapshot.
            if new_summary is not None and new_summary != old_summary:
                self._summary_boundary = len(self.messages)

    async def shrink_view(self, context: ContextGenome, *,
                          trace: Any = None,
                          iteration: int | None = None,
                          factor: float = 0.7) -> bool:
        """Force the view below the size the provider just rejected.

        Estimates can undershoot, and when the provider answers 400 the
        estimate is not the ruler that matters: rebuild against 70% of the
        *measured* size.  Returns whether the view got strictly smaller;
        ``False`` means a resend would be rejected again.
        """
        current = request_view_size(self.view,
                                    self._budget(context, self._usage_anchor))
        # Measure with the PRE-refresh anchor: refresh clears it, and
        # comparing sizes taken with different rulers would misjudge.
        anchor_before = self._usage_anchor
        target = max(1, int(current * factor))
        before = self.view
        try:
            await self.refresh_view(context, trace=trace, iteration=iteration,
                                    force_budget=target)
        except Exception as exc:
            logger.warning(
                "llm.context_window.refresh_failed iteration=%s %s",
                iteration, exc)
            return False
        if self.view is before:
            # Nothing was rebuilt, so a resend would hit the same wall.
            return False
        after = request_view_size(self.view,
                                  self._budget(context, anchor_before))
        if after >= current:
            logger.warning("llm.context_window.no_shrink iteration=%s",
                           iteration)
            return False
        return True


__all__ = ["Conversation", "TurnHistory", "ensure_system_prompt"]
