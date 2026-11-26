"""Fail-fast budget for repeated identical tool errors.

The loop feeds every tool failure back as an observation and continues,
so a model stuck on one broken call (e.g. an ``apply_patch`` missing its
closing marker) burns all ``max_iterations``. This module counts
consecutive failures keyed by ``(tool, error signature)``: any success
or a different error resets the count, and reaching the configured
limit tells the loop to stop early with a manual-handling message.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from agent_runtime.runtime_config import RuntimeConfig

#: Runtime override for the ``recovery.tool_error_max_retries`` gene.
#: Read only by :meth:`RuntimeConfig.from_env`.
ENV_VAR = "AGENT_RUNTIME_TOOL_ERROR_MAX_RETRIES"

_SIGNATURE_LIMIT = 200

# "apply_patch: line 6: <reason>" and "line 9: <reason>" are the same
# failure; strip the volatile line-number prefix before comparing.
_LINE_PREFIX = re.compile(r"^(?:[\w/.-]+:\s*)?line \d+\s*:\s*",
                          re.IGNORECASE)


def error_signature(error: object) -> str:
    """Normalize an error to a short comparable signature."""
    text = str(error).strip().split("\n")[0].strip().lower()
    text = _LINE_PREFIX.sub("", text).strip()
    return text[:_SIGNATURE_LIMIT] if text else "unknown error"


def resolve_max_retries(harness_value: int,
                        override: int | None = None, *,
                        runtime: "RuntimeConfig | None" = None) -> int:
    """Harness gene value, overridable by an explicit runtime knob.

    Precedence: ``override``, then the injected ``runtime`` (its
    ``tool_error_max_retries``, unset meaning "keep the harness gene" —
    the environment is never consulted, so a caller that already resolved
    the config cannot be overruled by a later env change), then one
    ``RuntimeConfig.from_env()`` read for callers with no config to
    thread.  Negative values clamp to 0 (guard disabled); an unparseable
    env value is ignored.
    """
    if override is None:
        if runtime is not None:
            override = runtime.tool_error_max_retries
        else:
            from agent_runtime.runtime_config import RuntimeConfig

            override = RuntimeConfig.from_env().tool_error_max_retries
    if override is None:
        return harness_value
    return max(0, override)


class SameToolErrorBudget:
    """Count consecutive identical tool failures; 0 disables the guard."""

    def __init__(self, max_retries: int) -> None:
        self.max_retries = max(0, max_retries)
        self._key: tuple[str, str] | None = None
        self._count = 0

    @property
    def count(self) -> int:
        return self._count

    def note_success(self) -> None:
        self._key = None
        self._count = 0

    def note_failure(self, tool: str | None,
                     error: object) -> tuple[int, bool]:
        """Record a failure; return ``(consecutive_count, tripped)``."""
        key = (tool or "unknown tool", error_signature(error))
        if key == self._key:
            self._count += 1
        else:
            self._key, self._count = key, 1
        tripped = (self.max_retries > 0 and self._count >= self.max_retries)
        return self._count, tripped


__all__ = [
    "ENV_VAR",
    "SameToolErrorBudget",
    "error_signature",
    "resolve_max_retries",
]
