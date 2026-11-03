"""The finish half of a turn: how a turn is allowed to end.

The turn loop decides *when* to ask the model and which calls to execute;
this module decides whether a candidate finish is acceptable.  It owns
the whole submission contract:

* intercepting ``submit_result`` instead of executing it,
* shape and grounding checks on the typed parameters,
* re-running the declared command once and classifying the outcome,
* nudging plain-text or invalid finishes back to the contract,
* the finish-violation fuse, and
* the contract events recorded on success, rejection and abort.

The rerun helpers at the top are pure: they classify a command result and
render an excerpt.  Execution and tracing stay in
:meth:`TurnFinisher.rerun_declared_command`.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any

from agent_runtime.agent.tool_dispatch import ToolCallOutcome
from agent_runtime.agent.turn_failure import TurnFailure
from agent_runtime.agent.turn_history import TurnHistory
from agent_runtime.agent.verification_modes import get_finish_mode
from agent_runtime.agent.verification import (
    SubmissionRules,
    check_submission,
    contract_nudge,
    render_submission,
    submit_nudge,
)
from agent_runtime.events import EventEmitter
from agent_runtime.tools import ToolExecutor
from agent_runtime.trace import RunTrace

logger = logging.getLogger(__name__)

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


def _rerun_declaration_broken(exit_code: Any, excerpt: str) -> bool:
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


def _rerun_failure_detail(excerpt: str) -> str:
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


def _rerun_outcome(result: Any) -> tuple[bool, Any, str]:
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


class TurnFinisher:
    """Validate or reject a turn's finish attempt.

    One instance serves exactly one turn: consecutive violations are
    turn-local state, reset by any executed tool call.  The loop calls
    :meth:`note_work_executed` for that reset, so the fuse policy never
    leaks into the loop as a raw counter.
    """

    def __init__(self, *,
                 rules: SubmissionRules,
                 tools: ToolExecutor,
                 trace: RunTrace,
                 events: EventEmitter,
                 max_iterations: int,
                 finish_violation_limit: int) -> None:
        self.rules = rules
        self.tools = tools
        self.trace = trace
        self.events = events
        self.max_iterations = max_iterations
        self.finish_violation_limit = finish_violation_limit
        # Consecutive finish attempts rejected without interleaving work
        # (plain-text finishes, invalid submissions). At the limit the
        # turn aborts instead of burning the iteration budget on prose.
        self._finish_violations = 0

    def take_submit_arguments(
            self, outcomes: list[ToolCallOutcome]) -> dict[str, Any] | None:
        """First validated submit-call arguments, if this harness honors them.

        Any enabled verification mode intercepts the tool; ``off`` lets a
        call through to the placeholder handler.
        """
        if not get_finish_mode(self.rules.mode).intercepts_submit:
            return None
        for outcome in outcomes:
            if (outcome.validated is not None
                    and outcome.validated.name == self.rules.submit_tool):
                return dict(outcome.validated.arguments)
        return None

    def answer_without_tools(self, response: dict[str, Any], iteration: int,
                             history: TurnHistory) -> str | None:
        """Handle an answer with no tool calls; ``None`` means nudge + continue.

        Under ``verification.mode == "off"`` the plain answer ends the turn;
        an enabled harness only accepts a typed submit call, so a bare
        message is either the budget-exhausted fallback or a contract miss
        worth one nudge.
        """
        answer = response.get("content", "")
        if get_finish_mode(self.rules.mode).plain_answer_finishes:
            logger.debug("agent.final iteration=%d answer_chars=%d",
                         iteration, len(answer))
            self.events.emit("agent.completed", iteration, iterations=iteration,
                             answer=answer)
            return answer
        # Labs-OO-Agents BenchAgent pattern: a bare message can
        # never finish the turn — the model must submit via the
        # configured submit tool, whose parameters are typed. Legacy
        # return_contract prompts describe text sections, but the
        # runtime no longer accepts them either.
        if iteration >= self.max_iterations:
            # No budget left for a retry; accept as-is and record
            # the miss on the summary turn instead of overrunning.
            self.trace.emit("verification.failed", iteration,
                            missing=[self.rules.submit_tool],
                            reasons={self.rules.submit_tool:
                                     "budget exhausted without a valid "
                                     f"{self.rules.submit_tool} call"})
            logger.debug("agent.final iteration=%d answer_chars=%d "
                         "unsubmitted", iteration, len(answer))
            self.events.emit("agent.completed", iteration, iterations=iteration,
                             answer=answer)
            return answer
        self.note_finish_violation(iteration)
        gaps = {self.rules.submit_tool: "plain-text answer cannot finish "
                                       f"the task — call {self.rules.submit_tool}"}
        self.trace.emit("verification.failed", iteration,
                        missing=sorted(gaps), reasons=gaps)
        history.append({"role": "user", "content": submit_nudge(
            self.rules.submit_tool)})
        return None

    async def finish_or_nudge(self, submit_arguments: dict[str, Any],
                              iteration: int,
                              history: TurnHistory) -> str | None:
        """Validate the turn's submit attempt; ``None`` means nudge.

        The nudge is appended to the history and the loop continues; a
        rendered submission is the final answer of the turn.
        """
        nudge = await self.validate_submission(
            submit_arguments, iteration, history.messages)
        if nudge is not None:
            history.append({"role": "user", "content": nudge})
            return None
        answer = render_submission(submit_arguments)
        logger.debug("agent.final iteration=%d answer_chars=%d submit=True",
                     iteration, len(answer))
        self.events.emit("agent.completed", iteration, iterations=iteration,
                         answer=answer)
        return answer

    async def validate_submission(self, fields: dict[str, Any], iteration: int,
                                  messages: list[dict[str, Any]] | None = None
                                  ) -> str | None:
        """Validate a submit finish attempt; ``None`` means accept.

        Checks shape plus grounding on the typed parameters, then always
        re-executes a declared command once. Non-code harnesses (no
        ``command_to_verify`` declared) skip the rerun: there is nothing
        to confirm.
        """
        gaps = check_submission(fields, self.rules, messages)
        if gaps:
            self.note_finish_violation(iteration)
            self.trace.emit("verification.failed", iteration,
                            missing=sorted(gaps), reasons=gaps)
            return contract_nudge(gaps, self.rules.submit_tool)
        raw = fields.get("command_to_verify", "")
        command = raw if isinstance(raw, str) else str(raw)
        if not command.strip():
            self.trace.emit("verification.passed", iteration,
                            missing=[])
            return None
        return await self.rerun_declared_command(command, iteration)

    async def rerun_declared_command(self, command: str,
                                     iteration: int) -> str | None:
        """Re-execute the declared string verbatim once; accept only on exit 0.

        No history resolution: the declaration is authoritative (the prompt
        mandates exact-as-run, including any cd prefix), so what runs is
        exactly what was declared. The cheap was-run pre-filter already
        passed; this execution is the real confirmation and its exit code
        decides.
        """
        if not command.strip():
            gaps = {"command_to_verify": "missing or too short"}
            self.trace.emit("verification.failed", iteration,
                            missing=sorted(gaps), reasons=gaps)
            return contract_nudge(gaps, self.rules.submit_tool)
        try:
            result = await self.tools.execute(
                "run_command", {"command": command,
                                "timeout": _RERUN_TIMEOUT})
        except Exception as exc:
            gaps = {"command_to_verify":
                    f"rerun failed to execute ({exc}); the declared command "
                    "could not be executed, so nothing was verified"}
            self.trace.emit("verification.rerun", iteration, command=command,
                            success=False, error=str(exc))
            self.trace.emit("verification.failed", iteration,
                            missing=sorted(gaps), reasons=gaps)
            return contract_nudge(gaps, self.rules.submit_tool)
        success, exit_code, excerpt = _rerun_outcome(result)
        self.trace.emit("verification.rerun", iteration, command=command,
                        success=success, exit_code=exit_code, output=excerpt)
        if success:
            self.trace.emit("verification.passed", iteration,
                            missing=[], reran=True)
            return None
        if _rerun_declaration_broken(exit_code, excerpt):
            reason = ("declared command never reached the tests "
                      f"(rerun exited {exit_code}); the failure is in the "
                      "command itself (environment, interpreter, path or "
                      "shell syntax), not necessarily in the fix")
            gaps = {"command_to_verify": reason}
            self.trace.emit("verification.failed", iteration,
                            missing=sorted(gaps), reasons=gaps)
            nudge = (f"Your submission fails the return contract: "
                     f"command_to_verify ({reason}).")
            if excerpt:
                nudge += f" Rerun output of `{command}`: {excerpt}"
            return nudge
        detail = _rerun_failure_detail(excerpt)
        # Result + reason + acceptance criterion only: no fix target
        # (tests vs. source belongs to the task instructions) and no
        # prescribed action (re-running is wasteful when only the
        # declaration is wrong) — the model decides the next step.
        gaps = {"command_to_verify":
                f"rerun of your declared command exited {exit_code}{detail}; "
                "a submission is only accepted when that command exits 0"}
        self.trace.emit("verification.failed", iteration,
                        missing=sorted(gaps), reasons=gaps)
        nudge = contract_nudge(gaps, self.rules.submit_tool)
        if excerpt:
            nudge += f" Rerun output of `{command}`: {excerpt}"
        return nudge

    def note_finish_violation(self, iteration: int) -> None:
        """Count a rejected finish attempt; abort at the harness limit.

        Plain-text finishes and invalid submissions burn budget without
        progress, so consecutive violations (no executed tool call in
        between) abort the turn instead — the labs-OO-Agents
        ``max_consecutive_text_only`` pattern. ``0`` disables the fuse.
        """
        limit = self.finish_violation_limit
        if limit <= 0:
            return
        self._finish_violations += 1
        if self._finish_violations >= limit:
            self.trace.emit("verification.aborted", iteration,
                            consecutive=self._finish_violations)
            raise TurnFailure(
                f"ABORTED_REPEATED_FINISH_VIOLATIONS: {self._finish_violations} "
                f"consecutive finish attempts rejected without interleaving work "
                f"(limit {limit}). Finish by calling {self.rules.submit_tool} "
                f"with valid parameters after running the tests.")

    def note_work_executed(self) -> None:
        """Reset the fuse: an executed tool call produced new information."""
        self._finish_violations = 0

    def emit_contract_outcome(self, iteration: int) -> None:
        """Record the contract outcome on the budget-exhausted summary turn.

        The summary turn carries no tools, so a submission is impossible:
        the outcome is always a miss.
        """
        if not get_finish_mode(self.rules.mode).report_contract_miss:
            return
        self.trace.emit("verification.failed", iteration,
                        missing=[self.rules.submit_tool],
                        reasons={self.rules.submit_tool:
                                 "budget exhausted without a valid "
                                 f"{self.rules.submit_tool} call"})


__all__ = ["TurnFinisher"]
