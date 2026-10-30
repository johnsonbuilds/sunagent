"""Rerun helpers for the verification gate's declared-command re-execution.

Split out from ``agent.loop`` so the turn loop stays orchestration-only:
pure functions here decide broken-declaration vs test-failure and render
excerpts, while ``AgentTurn._rerun_declared_command`` owns the execution
and trace side effects.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

_RERUN_OUTPUT_CHARS = 1500
# Verify commands take minutes; the agent's own default (30s) would turn every
# honest rerun into a timeout failure. The timeout is a ceiling only: the
# rerun returns as soon as the process exits, so a broken declaration
# (e.g. "No module named pytest") fails fast instead of burning 600s.
_RERUN_TIMEOUT = 600.0

# Positive signals that a test runner actually ran: counts summaries,
# FAILED lines, collection reports. The rerun branch is decided by this —
# not by a hardcoded list of environment errors (which can never be
# exhaustive): without evidence that tests ran, the declaration itself is
# treated as broken. That default is safe: misjudged as broken, the model
# re-runs the command once and self-corrects; misjudged as a test failure,
# it would burn a long verify run and loop.
_TEST_RAN_RE = re.compile(
    r"\d+\s+(passed|failed|failing)\b|\bFAILED\b|--- FAIL\b"
    r"|\bFAIL\b|collected\s+\d+\s+item|test result:\s*\S*FAILED",
    re.IGNORECASE)


def rerun_declaration_broken(exit_code: Any, excerpt: str) -> bool:
    """The rerun never ran any test: broken declaration, not a test failure."""
    if exit_code is None:
        # No process result at all (tool-level failure, truncated
        # conda-activation string, …): nothing ran, nothing is verified.
        return True
    return _TEST_RAN_RE.search(excerpt) is None


_COUNTS_RE = re.compile(r"\d+\s+failed[^\n]*|\d+\s+passed[^\n]*in\s+[\d.]+s",
                        re.IGNORECASE)
_FAILED_ID_RE = re.compile(r"^(?:FAILED\s+|--- FAIL:\s*)(\S+)", re.MULTILINE)
_ERROR_LINE_RE = re.compile(
    r"^E\s+(.+)$|((?:Assertion|Value|Type|Key|Index|Attribute)Error[^\n]*)",
    re.MULTILINE)


def rerun_failure_detail(excerpt: str) -> str:
    """One-line specifics from a failing rerun: counts + failing test ids."""
    details: list[str] = []
    counts = _COUNTS_RE.search(excerpt)
    if counts:
        details.append(counts.group(0).strip())
    failing = _FAILED_ID_RE.findall(excerpt)[:5]
    if failing:
        details.append("failing: " + ", ".join(failing))
    error = _ERROR_LINE_RE.search(excerpt)
    if error:
        line = next(part for part in error.groups() if part)
        details.append(line.strip()[:200])
    return (": " + "; ".join(details)) if details else ""


def rerun_outcome(result: Any) -> tuple[bool, Any, str]:
    """Success flag, exit code, and a truncated output excerpt for a rerun."""
    if not isinstance(result, Mapping):
        return False, None, str(result)[:_RERUN_OUTPUT_CHARS]
    error = result.get("error")
    exit_code = result.get("exit_code")
    stdout = result.get("stdout") if isinstance(result.get("stdout"), str) else ""
    stderr = result.get("stderr") if isinstance(result.get("stderr"), str) else ""
    output = (f"stdout: {stdout} stderr: {stderr}").strip()
    if len(output) > _RERUN_OUTPUT_CHARS:
        # Failure summaries (counts, FAILED lines) sit at the END of test
        # output — keep head + tail so neither the model nor the
        # ran-or-broken classification loses them.
        head = _RERUN_OUTPUT_CHARS * 2 // 3
        tail = _RERUN_OUTPUT_CHARS - head
        output = output[:head] + "\n…[truncated]…\n" + output[-tail:]
    if error:
        return False, exit_code, output or str(error)[:_RERUN_OUTPUT_CHARS]
    return exit_code == 0, exit_code, output


__all__ = [
    "_RERUN_OUTPUT_CHARS",
    "_RERUN_TIMEOUT",
    "rerun_declaration_broken",
    "rerun_failure_detail",
    "rerun_outcome",
]
