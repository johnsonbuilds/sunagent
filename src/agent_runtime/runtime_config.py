"""Unified runtime configuration: one env parse, explicit injection.

All ``os.getenv`` reads for endpoint/transport knobs converge here.
The composition root (``turn_factory`` / ``run_agent`` / web UI / Harbor)
calls :meth:`RuntimeConfig.from_env` once; deep layers
(``TurnHistory``, ``context``, ``chat``) receive the resolved value
instead of reading the environment themselves.

This fixes the split-brain where ``OpenAICompatibleLLM(model=...,
base_url=...)`` was built with explicit args while the budget layer
recomputed ``count_reasoning`` from ``MODEL_ID``/``LLM_BASE_URL``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any


def _env_str(name: str, default: str) -> str:
    return os.getenv(name, default)


def _env_optional_str(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, str(default)))
    except ValueError:
        return default


def _env_flag(name: str, default: bool) -> bool:
    """``1``-by-default flags; ``0/false/no/off`` disables."""
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


@dataclass(frozen=True)
class RuntimeConfig:
    """Endpoint + transport knobs, resolved once at the composition root.

    ``HarnessSpec`` stays the source of truth for behavior genes
    (prompt/tools/control/context/recovery/verification); this object
    only carries process wiring that used to be scattered ``os.getenv``
    calls: which endpoint to hit and how to talk to it.
    """

    model: str
    base_url: str | None
    api_key: str | None = None
    temperature: float = 0.0
    reasoning_echo_mode: str = "auto"
    reasoning_effort: str | None = None
    stream_usage: bool = True
    streaming: bool = True
    stream_idle_timeout: float | None = 120.0
    max_reasoning_chars: int | None = 50_000
    summary_model: str | None = None

    @classmethod
    def from_env(cls, **overrides: Any) -> RuntimeConfig:
        """Read the environment once; non-``None`` overrides win.

        Accepts ``model``/``base_url``/``api_key``/``temperature`` and
        every other field name so call sites can forward explicit
        constructor args without re-reading ``os.environ``.
        """
        from agent_runtime.reasoning_echo import (
            DEFAULT_MODEL,
            reasoning_echo_mode,
        )

        def pick(name: str, env_value: Any) -> Any:
            value = overrides.get(name, None)
            return env_value if value is None else value

        raw_timeout = overrides.get("stream_idle_timeout", None)
        if raw_timeout is None:
            raw = os.getenv("AGENT_RUNTIME_STREAM_IDLE_TIMEOUT", "120")
            try:
                timeout: float | None = float(raw)
            except ValueError:
                timeout = 120.0
            timeout = timeout if timeout > 0 else None
        else:
            timeout = raw_timeout
            if isinstance(timeout, (int, float)) and timeout <= 0:
                timeout = None

        raw_budget = overrides.get("max_reasoning_chars", None)
        if raw_budget is None:
            raw = os.getenv("AGENT_RUNTIME_MAX_REASONING_CHARS", "50000")
            try:
                budget: int | None = int(raw)
            except ValueError:
                budget = 50_000
            budget = budget if budget > 0 else None
        else:
            budget = raw_budget
            if isinstance(budget, int) and budget <= 0:
                budget = None

        mode_override = overrides.get("reasoning_echo_mode", None)
        mode = (mode_override.strip().lower()
                if isinstance(mode_override, str) and mode_override.strip()
                else reasoning_echo_mode())
        if mode not in ("auto", "always", "never"):
            mode = "auto"

        temperature_override = overrides.get("temperature", None)
        if temperature_override is None:
            try:
                temperature = float(os.getenv("LLM_TEMPERATURE", "0.0"))
            except ValueError:
                temperature = 0.0
        else:
            temperature = float(temperature_override)

        effort_override = overrides.get("reasoning_effort", None)
        if effort_override is None:
            effort_raw = os.getenv("AGENT_RUNTIME_REASONING_EFFORT", "").strip()
            effort = effort_raw or None
        else:
            effort = (effort_override.strip() or None
                      if isinstance(effort_override, str) else effort_override)

        stream_usage_override = overrides.get("stream_usage", None)
        stream_usage = (_env_flag("AGENT_RUNTIME_STREAM_USAGE", True)
                        if stream_usage_override is None
                        else bool(stream_usage_override))

        streaming_override = overrides.get("streaming", None)
        streaming = (_env_flag("AGENT_RUNTIME_STREAM", True)
                     if streaming_override is None
                     else bool(streaming_override))

        return cls(
            model=pick("model", _env_str("MODEL_ID", DEFAULT_MODEL)),
            base_url=pick("base_url", _env_optional_str("LLM_BASE_URL")),
            api_key=pick("api_key", _env_optional_str("LLM_API_KEY")),
            temperature=temperature,
            reasoning_echo_mode=mode,
            reasoning_effort=effort,
            stream_usage=stream_usage,
            streaming=streaming,
            stream_idle_timeout=timeout,
            max_reasoning_chars=budget,
            summary_model=pick("summary_model",
                               _env_optional_str("AGENT_RUNTIME_SUMMARY_MODEL")),
        )

    def should_echo(self, model: str | None = None,
                    base_url: str | None = None) -> bool:
        """Wire-projection decision for this config's endpoint."""
        from agent_runtime.reasoning_echo import should_echo_reasoning

        return should_echo_reasoning(
            self.model if model is None else model,
            self.base_url if base_url is None else base_url,
            self.reasoning_echo_mode)

    @property
    def echo_reasoning(self) -> bool:
        """Whether assistant ``reasoning_content`` is replayed on the wire."""
        return self.should_echo()


def resolve_echo_reasoning(llm: Any | None,
                           runtime: RuntimeConfig | None = None) -> bool:
    """Single source of truth for ``count_reasoning`` when no explicit bool.

    Preference: ``llm.echo_reasoning`` (bound to the instance's own
    ``model``/``base_url``) > ``runtime.echo_reasoning`` > legacy
    env-backed default.  ``FakeLLM`` test doubles expose neither
    attribute and fall through to the env default, preserving their
    historical behavior.
    """
    candidate = getattr(llm, "echo_reasoning", None)
    if isinstance(candidate, bool):
        return candidate
    # ``runtime_config`` property is the explicit escape hatch for
    # adapters that do not expose ``echo_reasoning`` directly.
    config = getattr(llm, "runtime_config", None)
    if isinstance(config, RuntimeConfig):
        return config.echo_reasoning
    # Plain ``model``/``base_url`` attributes (e.g. minimal fakes):
    # compute with the runtime's echo mode when available so an
    # explicit mode pin still applies.
    model = getattr(llm, "model", None)
    base_url = getattr(llm, "base_url", None)
    if isinstance(model, str):
        from agent_runtime.reasoning_echo import should_echo_reasoning

        mode = (runtime.reasoning_echo_mode
                if runtime is not None else None)
        return should_echo_reasoning(model, base_url, mode)
    if runtime is not None:
        return runtime.echo_reasoning
    from agent_runtime.reasoning_echo import reasoning_echo_enabled

    return reasoning_echo_enabled()


__all__ = ["RuntimeConfig", "resolve_echo_reasoning"]
