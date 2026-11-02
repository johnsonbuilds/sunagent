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
# back to the API").  Matched against MODEL_ID and LLM_BASE_URL.  A
# gateway that proxies one of these under a neutral host is misclassified
# — pin AGENT_RUNTIME_REASONING_ECHO=always|never for such setups.
_ECHO_FAMILY_MARKERS: tuple[str, ...] = (
    "deepseek", "glm", "zhipu", "bigmodel",
    "kimi", "moonshot", "mimo", "xiaomi", "ling",
)

# Hosts that proxy other providers.  A gateway drops the assistant
# turn's reasoning fields before the request reaches the upstream
# model, so replaying them is a no-op: measured against OpenRouter,
# 6400 chars of replayed reasoning cost 0 extra prompt tokens and the
# model never saw the text (the same 6400 chars in ``content`` cost
# +1202 tokens).  Model-name matching cannot catch this — OpenRouter
# serves ``deepseek/...`` models, so the family heuristic above would
# replay reasoning the gateway is about to drop, and the context-budget
# layer would then charge for tokens that are never sent.
_GATEWAY_HOST_MARKERS: tuple[str, ...] = ("openrouter.ai",)


def reasoning_echo_mode() -> str:
    """Outbound reasoning-replay policy: ``auto``, ``always``, or ``never``.

    Read per request like the other env-backed knobs.  Unknown values fall
    back to ``auto`` so a typo never strands the run on a wrong policy.
    """
    value = os.getenv("AGENT_RUNTIME_REASONING_ECHO", "auto").strip().lower()
    return value if value in ("auto", "always", "never") else "auto"


def _endpoint_replays_reasoning(base_url: str | None) -> bool | None:
    """Whether the endpoint honours a replayed chain of thought.

    ``True``/``False`` for a host we know the behaviour of, ``None``
    when the host is unknown (the caller then falls back to the
    model-family heuristic).
    """
    host = (base_url or "").lower()
    if any(marker in host for marker in _GATEWAY_HOST_MARKERS):
        return False
    return None


def should_echo_reasoning(model: str, base_url: str | None,
                          mode: str | None = None) -> bool:
    """Whether assistant ``reasoning_content`` is replayed to the provider."""
    if mode is None:
        mode = reasoning_echo_mode()
    if mode == "always":
        return True
    if mode == "never":
        return False
    verdict = _endpoint_replays_reasoning(base_url)
    if verdict is not None:
        return verdict
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
