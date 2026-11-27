"""Request-view sizing: char counting, budget-unit conversion, and the one
valuation object every budget decision reads.

Pure helpers with no strategy logic, shared by the deterministic compactor and
the LLM summarizer so budget accounting stays identical either way.

:class:`ViewBudget` is the single carrier of "how is this view measured":

* ``limit`` / ``unit`` — the genome's budget and its unit,
* ``anchor`` — the measured-usage anchor of the last billed response,
* ``echo_reasoning`` — whether reasoning chars ride the wire (and therefore
  must be counted).

It is built once per refresh by :class:`~agent_runtime.agent.turn_history.
TurnHistory` and handed down as one object, so no layer below the composition
root re-derives the echo policy or re-reads the environment.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any


def _message_chars(message: dict[str, Any],
                   echo_reasoning: bool = True) -> int:
    # reasoning_content rides the wire only when the echo policy replays it:
    # count it then, or long-thinking models outrun compaction without ever
    # tripping it.  When the endpoint drops it, counting would overestimate
    # the request and compact too early.
    total = len(message.get("content") or "")
    if echo_reasoning:
        total += len(message.get("reasoning_content") or "")
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        total += len(function.get("arguments") or "")
    return total


def total_chars(messages: list[dict[str, Any]],
                echo_reasoning: bool = True) -> int:
    return sum(_message_chars(message, echo_reasoning)
               for message in messages)


def _estimate_tokens(chars: int, chars_per_token: float = 4.0) -> int:
    """Char-based token estimate; code-heavy mixed text runs ~3-5 c/t."""
    return max(1, int(chars / chars_per_token)) if chars else 0


def _view_size(messages: list[dict[str, Any]], echo_reasoning: bool,
               unit: str,
               anchor: tuple[int, int, int] | None = None) -> int:
    """Request-view size in the genome's budget unit.

    ``chars`` (legacy) counts characters.  ``tokens`` extrapolates from the
    usage anchor ``(measured prompt tokens, view length, view chars)`` of the
    last billed response: at the anchor's view length the provider's own number
    is the truth, above it the tail is estimated with the ratio the anchor
    itself calibrates — no tokenizer dependency, and the ratio converges as
    measurements accumulate.
    """
    if unit != "tokens":
        return total_chars(messages, echo_reasoning)
    if anchor is None:
        return _estimate_tokens(total_chars(messages, echo_reasoning))
    tokens, anchor_len, anchor_chars = anchor
    ratio = tokens / anchor_chars if anchor_chars > 0 else 0.25
    if len(messages) == anchor_len:
        # Exactly the measured request: no conversion noise.
        return max(1, tokens)
    if len(messages) < anchor_len:
        # View only shrank since the measurement: scale the measured number
        # down by the calibrated ratio.
        return max(1, int(total_chars(messages, echo_reasoning) * ratio))
    tail_chars = total_chars(messages[anchor_len:], echo_reasoning)
    return tokens + int(tail_chars * ratio)


def _anchor_chars_per_token(anchor: tuple[int, int, int] | None) -> float:
    """Char-per-token ratio implied by the usage anchor (default 4.0)."""
    if anchor is None:
        return 4.0
    tokens, _, anchor_chars = anchor
    if tokens <= 0 or anchor_chars <= 0:
        return 4.0
    return anchor_chars / tokens


@dataclass(frozen=True)
class ViewBudget:
    """How one request view is measured, resolved once per refresh.

    Immutable on purpose: ``with_limit`` / ``with_anchor`` return a new value,
    so a hard-gate shrink cannot leak into the next refresh.
    """

    limit: int
    unit: str = "chars"
    anchor: tuple[int, int, int] | None = None
    echo_reasoning: bool = True

    @classmethod
    def for_context(cls, context: Any, *,
                    anchor: tuple[int, int, int] | None = None,
                    echo_reasoning: bool = True) -> "ViewBudget":
        """Read the genome's budget knobs into one valuation.

        ``context`` is only duck-typed (``context_budget`` / ``budget_unit``),
        so this module keeps no dependency on the harness layer.  The default
        ``echo_reasoning=True`` deliberately counts every reasoning char:
        callers that know the endpoint drops it pass ``False`` explicitly.
        """
        return cls(limit=context.context_budget, unit=context.budget_unit,
                   anchor=anchor, echo_reasoning=echo_reasoning)

    def with_limit(self, limit: int) -> "ViewBudget":
        return replace(self, limit=limit)

    def with_anchor(self, anchor: tuple[int, int, int] | None) -> "ViewBudget":
        return replace(self, anchor=anchor)

    def chars(self, messages: list[dict[str, Any]]) -> int:
        """Characters of ``messages`` under this valuation's echo policy."""
        return total_chars(messages, self.echo_reasoning)

    def size(self, messages: list[dict[str, Any]]) -> int:
        """Size of ``messages`` in this valuation's budget unit."""
        return _view_size(messages, self.echo_reasoning, self.unit, self.anchor)

    @property
    def chars_per_token(self) -> float:
        """Char-per-token ratio implied by the anchor (default 4.0)."""
        return _anchor_chars_per_token(self.anchor)

    @property
    def char_limit(self) -> int:
        """``limit`` expressed in chars, for char-metered strategies."""
        if self.unit != "tokens":
            return self.limit
        return max(1, int(self.limit * self.chars_per_token))


def request_view_size(view: list[dict[str, Any]], budget: ViewBudget) -> int:
    """Request-view size in the genome's unit, for gate decisions."""
    return budget.size(view)


__all__ = [
    "ViewBudget", "request_view_size", "total_chars",
]
