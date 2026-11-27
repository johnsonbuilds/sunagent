"""Reasoning echo policy shared by the provider adapter and context budget.

Reasoning replay has two consumers that must agree exactly:

* the provider projector, which decides whether assistant
  ``reasoning_content`` is sent back on the wire, and
* the context-budget layer, which must charge for reasoning chars only
  when they will actually be replayed.

Keeping the decision here — with no dependency on the provider adapter or
on the agent package — is what keeps those two numbers from drifting.
"""

from __future__ import annotations

import os

DEFAULT_MODEL = "glm-5.3"

# Substring markers for "echo" families: thinking-mode APIs that expect
# the assistant `reasoning_content` back on every replay (DeepSeek's
# contract: "the reasoning_content in the thinking mode must be passed
# back to the API"; the native API rejects a tool-call replay without it
# with a 400).  Matched against MODEL_ID, so a gateway that serves these
# models under ``vendor/model`` ids is matched too.  That is intended:
# gateways such as OpenRouter normalise the replayed field per upstream
# and do concatenate it, but *only when the request carries ``tools``* —
# which every agent-loop iteration does.  A same-shape probe without
# ``tools`` measures +0 prompt tokens and reads as "the gateway drops
# reasoning"; that measurement is what the former gateway denylist was
# based on, and it was wrong for the loop.
_ECHO_FAMILY_MARKERS: tuple[str, ...] = (
    "deepseek", "glm", "zhipu", "bigmodel",
    "kimi", "moonshot", "mimo", "xiaomi", "ling",
)


def reasoning_echo_mode() -> str:
    """Outbound reasoning-replay policy: ``auto``, ``always``, or ``never``.

    Read per request like the other env-backed knobs.  Unknown values fall
    back to ``auto`` so a typo never strands the run on a wrong policy.
    """
    value = os.getenv("AGENT_RUNTIME_REASONING_ECHO", "auto").strip().lower()
    return value if value in ("auto", "always", "never") else "auto"


def should_echo_reasoning(model: str, base_url: str | None,
                          mode: str | None = None) -> bool:
    """Whether assistant ``reasoning_content`` is replayed to the provider."""
    if mode is None:
        mode = reasoning_echo_mode()
    if mode == "always":
        return True
    if mode == "never":
        return False
    haystack = f"{model} {base_url or ''}".lower()
    return any(marker in haystack for marker in _ECHO_FAMILY_MARKERS)


def reasoning_echo_enabled(model: str | None = None,
                           base_url: str | None = None) -> bool:
    """Echo decision for the configured endpoint (env-backed defaults)."""
    return should_echo_reasoning(
        model if model is not None else os.getenv("MODEL_ID", DEFAULT_MODEL),
        base_url if base_url is not None else os.getenv("LLM_BASE_URL"))


__all__ = [
    "DEFAULT_MODEL",
    "reasoning_echo_enabled",
    "reasoning_echo_mode",
    "should_echo_reasoning",
]
