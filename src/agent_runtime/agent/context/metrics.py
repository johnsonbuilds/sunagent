"""Request-view size metrics: char counting and budget-unit conversion.

Pure helpers with no strategy logic. Both the deterministic compactor
and the LLM summarizer measure through here so budget accounting stays
identical whichever strategy runs.
"""

from __future__ import annotations

from typing import Any

from agent_runtime.reasoning_echo import reasoning_echo_enabled


def _count_reasoning(enabled: bool | None) -> bool:
    """Explicit policy wins; otherwise read the env-backed wire default.

    Production callers (``TurnHistory``) always pass the boolean resolved
    from ``RuntimeConfig``/``llm.echo_reasoning`` so budget accounting
    matches the wire projection.  Direct callers (tests, one-off scripts)
    keep the historical env-backed behavior.
    """
    return reasoning_echo_enabled() if enabled is None else enabled


def _message_chars(message: dict[str, Any],
                   count_reasoning: bool = True) -> int:
    # reasoning_content rides the wire on echo-family providers, so it
    # must count toward the context budget there — otherwise
    # long-thinking models outrun compaction without ever tripping it.
    # When the echo policy strips it, counting it would overestimate the
    # request and compact observations too early.
    total = len(message.get("content") or "")
    if count_reasoning:
        total += len(message.get("reasoning_content") or "")
    for call in message.get("tool_calls") or []:
        function = call.get("function") or {}
        total += len(function.get("arguments") or "")
    return total


def total_chars(messages: list[dict[str, Any]],
                count_reasoning: bool = True) -> int:
    return sum(_message_chars(message, count_reasoning)
               for message in messages)


def _estimate_tokens(chars: int, chars_per_token: float = 4.0) -> int:
    """Char-based token estimate; code-heavy mixed text runs ~3-5 c/t."""
    return max(1, int(chars / chars_per_token)) if chars else 0


def _view_size(messages: list[dict[str, Any]], count_reasoning: bool,
               unit: str,
               anchor: tuple[int, int, int] | None = None) -> int:
    """Request-view size in the genome's budget unit.

    ``chars`` (legacy) counts characters.  ``tokens`` extrapolates from
    the usage anchor ``(measured prompt tokens, view length, view chars)``
    produced by the last billed response: below the anchor's view length
    the provider's own number is the truth, above it the tail is
    estimated with the char ratio the anchor itself calibrates — no
    tokenizer dependency, and the ratio converges as measurements
    accumulate.
    """
    if unit != "tokens":
        return total_chars(messages, count_reasoning)
    if anchor is None:
        return _estimate_tokens(total_chars(messages, count_reasoning))
    tokens, anchor_len, anchor_chars = anchor
    ratio = tokens / anchor_chars if anchor_chars > 0 else 0.25
    if len(messages) == anchor_len:
        # Exactly the measured request: the provider's number is the
        # truth, no conversion noise.
        return max(1, tokens)
    if len(messages) < anchor_len:
        # View only shrank since the measurement: scale the measured
        # number down by the calibrated ratio.
        return max(1, int(total_chars(messages, count_reasoning) * ratio))
    tail_chars = total_chars(messages[anchor_len:], count_reasoning)
    return tokens + int(tail_chars * ratio)


def _anchor_chars_per_token(anchor: tuple[int, int, int] | None) -> float:
    """Char-per-token ratio implied by the usage anchor (default 4.0)."""
    if anchor is None:
        return 4.0
    tokens, _, anchor_chars = anchor
    if tokens <= 0 or anchor_chars <= 0:
        return 4.0
    return anchor_chars / tokens


def request_view_size(view: list[dict[str, Any]], context: Any,
                      usage_anchor: tuple[int, int, int] | None = None, *,
                      count_reasoning: bool | None = None) -> int:
    """Request-view size in the genome's unit, for gate decisions."""
    return _view_size(view, _count_reasoning(count_reasoning),
                      context.budget_unit, usage_anchor)


__all__ = [
    "request_view_size", "total_chars",
]
