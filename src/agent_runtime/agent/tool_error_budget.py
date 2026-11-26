"""Fail-fast budget for repeated identical tool errors.

The loop feeds every tool failure back as an observation and continues,
so a model stuck on one broken call (e.g. an ``apply_patch`` missing its
closing marker) burns all ``max_iterations``. This module counts
consecutive failures keyed by ``(tool, error signature)``: any success
or a different error resets the count, and reaching the configured
limit tells the loop to stop early with a manual-handling message.

The limit is the ``recovery.tool_error_max_retries`` harness gene and
nothing else: there is no runtime/environment override, so an injected
harness is the single source of truth.
"""

from __future__ import annotations

import re

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
    "SameToolErrorBudget",
    "error_signature",
]
