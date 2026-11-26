"""Unified runtime configuration: one env parse, explicit injection.

Boundary contract:
- ``HarnessSpec`` = agent policy (prompt/tools/control/context/recovery/
  skills). It enters ``genes_hash`` and is the only lineage/A-B axis.
- ``RuntimeConfig`` = run conditions (model, endpoint, transport, paths,
  resource limits, safety brakes). It is recorded in ``run.config`` and
  must never enter ``genes_hash``.
Each knob has exactly one home; env must never silently override a harness
gene.

All ``os.getenv`` reads for endpoint/transport knobs converge here.  The
composition root (``turn_factory`` / ``run_agent`` / web UI / Harbor) calls
:meth:`RuntimeConfig.from_env` once; deep layers (``TurnHistory``,
``context``, ``llm_request``) receive the resolved value instead of reading
the environment themselves.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

#: Guard policies accepted by :class:`execution.guard.CommandGuard`.
GUARD_MODES = ("off", "blocklist", "allowlist")


def _env_str(name: str, default: str) -> str:
    return os.getenv(name, default)


def _env_optional_str(name: str) -> str | None:
    value = os.getenv(name)
    if value is None:
        return None
    value = value.strip()
    return value or None


def _env_flag(name: str, default: bool) -> bool:
    """``1``-by-default flags; ``0/false/no/off`` disables."""
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _env_truthy(name: str, default: bool = False) -> bool:
    """``off``-by-default flags: only ``1/true/on`` enable."""
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "on"}


@dataclass(frozen=True)
class RuntimeConfig:
    """Run conditions, resolved once at the composition root.

    See the module docstring for the ``RuntimeConfig`` vs ``HarnessSpec``
    contract: behavior genes stay in ``HarnessSpec`` / ``genes_hash``; this
    object only carries the machine/endpoint/deployment facts needed to
    execute them.
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
    retry_4xx: bool = False
    summary_model: str | None = None
    # Run wiring that used to be read deep inside the execution/memory
    # layers: resolved here, passed down explicitly.
    workspace_root: str | None = None
    command_guard_mode: str = "blocklist"
    memory_enabled: bool = False
    memory_dir: str | None = None
    harness: str | None = None

    @classmethod
    def from_env(cls, **overrides: Any) -> RuntimeConfig:
        """Read the environment once; non-``None`` overrides win.

        Accepts every field name so call sites can forward explicit
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

        # Escape hatch: a few gateways report transient failures as 400.
        # Off by default so a rejected request fails on the first attempt.
        retry_4xx_override = overrides.get("retry_4xx", None)
        retry_4xx = (_env_flag("AGENT_RUNTIME_RETRY_4XX", False)
                     if retry_4xx_override is None
                     else bool(retry_4xx_override))

        guard_override = overrides.get("command_guard_mode", None)
        guard_mode = (_env_str("AGENT_RUNTIME_COMMAND_GUARD", "blocklist").strip().lower()
                      if guard_override is None
                      else str(guard_override).strip().lower())
        if guard_mode not in GUARD_MODES:
            # An unparseable policy must not make every executor construction
            # raise; fall back to the safe default once.
            logger.warning("runtime_config.invalid_command_guard_mode value=%r",
                           guard_mode)
            guard_mode = "blocklist"

        memory_override = overrides.get("memory_enabled", None)
        memory_enabled = (_env_truthy("AGENT_RUNTIME_MEMORY", False)
                          if memory_override is None
                          else bool(memory_override))

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
            retry_4xx=retry_4xx,
            summary_model=pick("summary_model",
                               _env_optional_str("AGENT_RUNTIME_SUMMARY_MODEL")),
            workspace_root=pick("workspace_root",
                                _env_optional_str("AGENT_RUNTIME_WORKSPACE")),
            command_guard_mode=guard_mode,
            memory_enabled=memory_enabled,
            memory_dir=pick("memory_dir",
                            _env_optional_str("AGENT_RUNTIME_MEMORY_DIR")),
            harness=pick("harness", _env_optional_str("AGENT_RUNTIME_HARNESS")),
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
    ``model``/``base_url``) > ``runtime.echo_reasoning`` > legacy env-backed
    default.  ``FakeLLM`` test doubles expose neither attribute and fall
    through to the env default.
    """
    candidate = getattr(llm, "echo_reasoning", None)
    if isinstance(candidate, bool):
        return candidate
    # ``runtime_config`` property is the explicit escape hatch for adapters
    # that do not expose ``echo_reasoning`` directly.
    config = getattr(llm, "runtime_config", None)
    if isinstance(config, RuntimeConfig):
        return config.echo_reasoning
    # Plain ``model``/``base_url`` attributes (e.g. minimal fakes): compute
    # with the runtime's echo mode when available so an explicit pin applies.
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
