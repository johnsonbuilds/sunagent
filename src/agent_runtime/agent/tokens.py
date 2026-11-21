"""Billed-token parsing shared by the turn loop and sub-calls.

Single home for ``response.get("usage")`` coercion so the main LLM path
(``agent/loop.py``) and the summarizer path (``agent/context.py``) report
the same numbers. Missing or partial ``usage`` counts as 0 — the ledger
stays additive and a fake without usage never breaks a turn.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any


def _cached_count(usage: Any) -> int:
    """Read the prompt-cache hit count from any provider shape.

    Accepts the flat key this runtime's own provider adapter emits, plus
    the raw forms a direct provider response can carry:
    ``prompt_tokens_details.cached_tokens`` (OpenAI) and
    ``prompt_cache_hit_tokens`` (DeepSeek-native).
    """
    if not isinstance(usage, Mapping):
        return 0

    def read(source: Any, name: str) -> int:
        if not isinstance(source, Mapping):
            return 0
        try:
            return max(0, int(source.get(name) or 0))
        except (TypeError, ValueError):
            return 0

    for value in (read(usage, "cached_tokens"),
                  read(usage.get("prompt_tokens_details"), "cached_tokens"),
                  read(usage, "prompt_cache_hit_tokens")):
        if value:
            return value
    return 0


def response_usage(response: Any) -> tuple[int, int, int]:
    """Prompt/completion/cached ints from an LLM ``chat`` response.

    ``cached`` is the part of ``prompt`` the provider served from its
    prompt cache (OpenAI ``prompt_tokens_details.cached_tokens``,
    DeepSeek ``prompt_cache_hit_tokens``); it is reported, never added.
    """
    usage = response.get("usage") if isinstance(response, dict) else None
    if not isinstance(usage, Mapping):
        return 0, 0, 0

    def count(name: str) -> int:
        try:
            return max(0, int(usage.get(name) or 0))
        except (TypeError, ValueError):
            return 0

    return count("prompt_tokens"), count("completion_tokens"), _cached_count(usage)


__all__ = ["response_usage"]
