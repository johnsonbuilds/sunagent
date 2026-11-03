"""Billed-token parsing shared by the turn loop and sub-calls.

Single home for ``response.get("usage")`` coercion so the main LLM path
(``agent/loop.py``) and the summarizer path (``agent/context.py``) report
the same numbers. Missing or partial ``usage`` counts as 0 — the ledger
stays additive and a fake without usage never breaks a turn.
"""

from __future__ import annotations

from typing import Any


def response_usage(response: Any) -> tuple[int, int]:
    """Prompt/completion ints from an LLM ``chat`` response."""
    usage = response.get("usage") if isinstance(response, dict) else None
    if not isinstance(usage, dict):
        return 0, 0
    try:
        prompt = int(usage.get("prompt_tokens") or 0)
    except (TypeError, ValueError):
        prompt = 0
    try:
        completion = int(usage.get("completion_tokens") or 0)
    except (TypeError, ValueError):
        completion = 0
    return max(0, prompt), max(0, completion)


__all__ = ["response_usage"]
