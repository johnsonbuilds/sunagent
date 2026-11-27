"""Reasoning replay policy: the model-family default.

Whether an assistant turn's ``reasoning_content`` is replayed is a property of
the model/endpoint, not of the machine or deployment, so it is not an env knob
and carries no gateway allow/deny list:

* a model catalog entry pins it explicitly (``ModelEntry.echo_reasoning``),
* otherwise this module supplies one pure default.

Thinking-mode families expect their chain of thought back on every replay
(DeepSeek's native API rejects a tool-call replay without it, HTTP 400).  The
playback only takes effect when the request carries ``tools`` — which every
agent-loop iteration does; a same-shape probe without ``tools`` measures zero
extra prompt tokens and reads as "the endpoint drops reasoning", which is the
measurement error that produced the former OpenRouter denylist.

Keeping the fallback a pure function is what lets the provider adapter, the
context-budget layer and post-mortem tooling agree without a second source.
"""

from __future__ import annotations

# Substring markers for "echo" families: thinking-mode APIs that expect the
# assistant `reasoning_content` back on every replay (DeepSeek's contract:
# "the reasoning_content in the thinking mode must be passed back to the
# API").  Matched against MODEL_ID, so a gateway that serves these models
# under ``vendor/model`` ids is matched too — that is intended: gateways such
# as OpenRouter normalise the replayed field per upstream and do concatenate
# it when the request carries ``tools``.
_ECHO_FAMILY_MARKERS: tuple[str, ...] = (
    "deepseek", "glm", "zhipu", "bigmodel",
    "kimi", "moonshot", "mimo", "xiaomi", "ling",
)


def derives_echo_reasoning(model: str | None, base_url: str | None) -> bool:
    """Family-default replay decision for a model with no catalog override."""
    haystack = f"{model or ''} {base_url or ''}".lower()
    return any(marker in haystack for marker in _ECHO_FAMILY_MARKERS)


__all__ = ["derives_echo_reasoning"]
