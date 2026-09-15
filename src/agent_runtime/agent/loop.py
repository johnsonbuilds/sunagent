"""The minimal tool-calling loop, independent of providers and tools.

Tool calls are validated before they enter the conversation history: only
canonical, validated calls are replayed to the provider, so one malformed
tool call cannot poison later requests.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping
from typing import Any

from agent_runtime.agent.chat import (
    ChatModel,
    _ReasoningBudgetExceeded,
    chat_with_recovery,
    use_streaming,
)
from agent_runtime.agent.tool_dispatch import (
    ToolCallOutcome,
    ValidatedToolCall,
    canonical_tool_calls,
    classify_tool_calls,
    validate_tool_call,
    _rejected_observation_content,
    _rejected_observation_message,
    _tool_call_payload,
    _tool_observation_message,
    _tool_result_summary,
    _tool_trace_metadata,
    _trace_tool_rejection,
)
from agent_runtime.agent.tool_error_budget import (
    SameToolErrorBudget,
    resolve_max_retries,
)
from agent_runtime.agent.verification import (
    check_contract,
    contract_nudge,
    executed_commands,
    extract_field_value,
    match_executed_command,
    render_submission,
    submit_nudge,
)
from agent_runtime.agent.observations import ObservationFormatter
from agent_runtime.agent.turn_history import (
    Conversation,
    TurnHistory,
    _ensure_system_prompt,
)
from agent_runtime.events import EventEmitter
from agent_runtime.harness import HarnessSpec, default_harness
from agent_runtime.tools import ToolExecutor
from agent_runtime.trace import RunTrace

logger = logging.getLogger(__name__)

# Finish-signal tool honored only under a task_result verification
# harness; elsewhere it is inert registry data.
SUBMIT_TOOL = "submit_result"

_RERUN_OUTPUT_CHARS = 1500


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
        output = output[:_RERUN_OUTPUT_CHARS] + "…"
    if error:
        return False, exit_code, output or str(error)[:_RERUN_OUTPUT_CHARS]
    return exit_code == 0, exit_code, output


class _TurnFailure(Exception):
    """An expected turn failure that should be returned to the caller."""


class AgentTurn:
    """One agent turn: collaborators and policy bound as attributes.

    ``run`` drives the tool-calling loop; helpers read ``self`` instead
    of threading a dozen parameters through every call.
    """

    def __init__(self, user_message: str, llm: ChatModel, tools: ToolExecutor,
                 max_iterations: int | None = None, *,
                 harness: HarnessSpec | None = None,
                 conversation: Conversation | None = None,
                 stream: bool = False,
                 trace: RunTrace | None = None,
                 events: EventEmitter | None = None) -> None:
        self.harness = harness or default_harness()
        self.max_iterations = (
            max_iterations if max_iterations is not None
            else self.harness.control.max_iterations)
        self.trace = trace or RunTrace(harness=self.harness)
        self.events = events or EventEmitter(run_id=self.trace.run_id)
        self.tools = tools
        # The tool executor owns the workspace (ToolRegistry binds it for
        # its file tools); observation spilling shares that same instance
        # so references and tools always agree on where data lives.  Its
        # knobs come from the harness genome; archive_min_chars derives
        # from memory.head_chars so the invariant is structural.
        self.observations = ObservationFormatter(
            getattr(self.tools, "workspace", None),
            max_chars=self.harness.control.max_observation_chars,
            spill_preview_chars=self.harness.control.spill_preview_chars,
            archive_min_chars=self.harness.memory.head_chars)
        self.user_message = user_message
        self.llm = llm
        self.conversation = conversation or Conversation()
        self.stream = stream

    async def run(self) -> str:
        """Execute the turn and always flush the trace."""
        agent_meta: dict[str, Any] = {}
        try:
            with self.trace.span("agent") as agent_meta:
                return await self._run(agent_meta)
        except _TurnFailure as exc:
            self.events.emit("runtime.error",
                             stage=agent_meta.get("stage", "agent"),
                             error=str(exc))
            return str(exc)
        except Exception as exc:
            self.events.emit("runtime.error",
                             stage=agent_meta.get("stage", "agent"),
                             error=f"{type(exc).__name__}: {exc}")
            raise
        finally:
            await self.trace.flush()

    async def _run(self, agent_meta: dict[str, Any]) -> str:
        events, trace = self.events, self.trace
        tools, harness = self.tools, self.harness

        events.emit("agent.started", message=self.user_message)
        _ensure_system_prompt(self.conversation, harness.prompt.system)
        self.conversation.append({"role": "user", "content": self.user_message})
        history = TurnHistory(self.conversation.messages)
        error_budget = SameToolErrorBudget(
            resolve_max_retries(harness.recovery.tool_error_max_retries))

        for iteration in range(1, self.max_iterations + 1):
            try:
                await history.refresh_view(harness.memory,
                                           trace=trace, iteration=iteration)
                logger.debug(
                    "llm.request.start iteration=%d messages=%d tools=%d stream=%s",
                    iteration, len(history.messages), len(tools.schemas), self.stream)
                with trace.span("llm", iteration,
                                message_count=len(history.view),
                                tool_count=len(tools.schemas),
                                messages=list(history.view)) as span_meta:
                    response = await chat_with_recovery(
                        self.llm, history.view, tools.schemas, iteration,
                        harness, stream=self.stream, trace=trace, events=events)
                    tool_calls = response.get("tool_calls") or []
                    content = response.get("content") or ""

                    span_meta.update(
                        tool_count=len(tool_calls),
                        tools=[_tool_trace_metadata(call).get("tool")
                               for call in tool_calls],
                        final=not tool_calls,
                    )
                    logger.debug(
                        "llm.request.end iteration=%d content_chars=%d tool_calls=%s",
                        iteration, len(content),
                        [_tool_trace_metadata(call).get("tool") for call in tool_calls])
            except _ReasoningBudgetExceeded as exc:
                # The model spiralled into thinking without acting. Append
                # an assistant placeholder plus a nudge and retry on the
                # next iteration (consuming one iteration of the budget) —
                # resending identical input would just reproduce the spiral.
                logger.warning(
                    "llm.reasoning_budget_exceeded iteration=%d %s", iteration, exc)
                history.append({"role": "assistant",
                                "content": "(extended reasoning omitted)"})
                nudge = {
                    "role": "user",
                    "content": (
                        f"Your last turn produced {exc.reasoning_chars} characters "
                        f"of reasoning without any content or tool calls. Stop "
                        f"reasoning and act now: call a tool, or give your "
                        f"final answer."),
                }
                history.append(nudge)
                continue
            except Exception as exc:
                logger.error("llm.request.error iteration=%d error=%s", iteration, exc)
                error = f"LLM error: {exc}"
                agent_meta.update(stage="llm", error=error)
                raise _TurnFailure(error) from exc

            outcomes = classify_tool_calls(tool_calls, tools.schemas)
            # A submit_result call is a finish attempt, not work: other
            # calls in the same turn still execute first, then the first
            # submission is validated against the updated history.
            submit_args = self._take_submit_args(outcomes)

            assistant_message: dict[str, Any] = {
                "role": "assistant",
                "content": content,
            }
            reasoning_content = response.get("reasoning_content")
            if reasoning_content:
                assistant_message["reasoning_content"] = reasoning_content
            canonical_calls = canonical_tool_calls(outcomes)
            if canonical_calls:
                assistant_message["tool_calls"] = canonical_calls
            if tool_calls or content or reasoning_content:
                history.append(assistant_message)

            if not tool_calls:
                answer = response.get("content", "")
                if (self.harness.verification.mode == "task_result"
                        and iteration < self.max_iterations):
                    # Labs-OO-Agents BenchAgent pattern: a bare message
                    # cannot end the turn — the model must submit via the
                    # submit_result tool, whose parameters are typed.
                    gaps = {"submit_result": "plain-text answer cannot finish "
                                             "the task — call submit_result"}
                    self.trace.emit("verification.failed", iteration,
                                    missing=sorted(gaps), reasons=gaps)
                    history.append({"role": "user", "content": submit_nudge()})
                    continue
                nudge = await self._contract_nudge(answer, iteration, history.messages)
                if nudge is not None:
                    history.append({"role": "user", "content": nudge})
                    continue
                logger.debug("agent.final iteration=%d answer_chars=%d",
                             iteration, len(answer))
                events.emit("agent.completed", iteration, iterations=iteration, answer=answer)
                return answer
            for outcome in outcomes:
                if (submit_args is not None and outcome.validated is not None
                        and outcome.validated.name == SUBMIT_TOOL):
                    continue  # finish attempt, validated below
                tool_call = outcome.tool_call
                tool_meta = _tool_trace_metadata(tool_call)
                logger.debug("tool.dispatch iteration=%d tool=%r tool_call_id=%r",
                             iteration, tool_meta.get("tool"),
                             tool_meta.get("tool_call_id"))
                payload = _tool_call_payload(tool_call)
                events.emit("tool.started", iteration, call_id=payload["call_id"],
                            tool=payload["tool"], arguments=payload["arguments"])
                tool_started = time.monotonic()
                if outcome.rejection is not None:
                    observation = self._reject_tool_call(outcome, iteration)
                    history.append(observation)
                    stop = self._check_error_budget(
                        error_budget, iteration,
                        payload["tool"], outcome.rejection)
                    if stop is not None:
                        return stop
                    continue
                validated = outcome.validated
                assert validated is not None
                try:
                    with trace.span("tool", iteration, **tool_meta) as span_meta:
                        result = await tools.execute(validated.name,
                                                     validated.arguments)
                        span_meta.update(tool=validated.name,
                                         tool_call_id=validated.id)
                    logger.debug(
                        "tool.result iteration=%d tool=%r result_chars=%d",
                        iteration, validated.name, len(str(result)))
                    events.emit("tool.completed", iteration, call_id=payload["call_id"],
                                duration=round(time.monotonic() - tool_started, 3),
                                **_tool_result_summary(result))
                except Exception as exc:
                    logger.error("tool.error iteration=%d tool=%r error=%s",
                                 iteration, validated.name, exc)
                    events.emit("tool.failed", iteration, call_id=payload["call_id"],
                                tool=payload["tool"], error=str(exc))
                    observation = harness.tool_error_observation(
                        exc, tool=payload["tool"])
                    history.append(_tool_observation_message(tool_call, observation))
                    stop = self._check_error_budget(
                        error_budget, iteration, validated.name, exc)
                    if stop is not None:
                        return stop
                    continue
                rendered = await self.observations.render(result, validated.id)
                metadata = ({"spill_path": rendered.spill_path}
                            if rendered.spill_path else None)
                history.append(_tool_observation_message(
                    tool_call, rendered.text, metadata))
                error_budget.note_success()

            if submit_args is not None:
                nudge = await self._validate_submission(
                    submit_args, iteration, history.messages)
                if nudge is not None:
                    history.append({"role": "user", "content": nudge})
                    continue
                answer = render_submission(submit_args)
                logger.debug("agent.final iteration=%d answer_chars=%d submit=True",
                             iteration, len(answer))
                events.emit("agent.completed", iteration, iterations=iteration,
                            answer=answer)
                return answer

        history.append({"role": "user", "content":
                        harness.prompt.iteration_limit_notice})
        iteration = self.max_iterations + 1
        try:
            await history.refresh_view(harness.memory,
                                       trace=trace, iteration=iteration)
            logger.debug("llm.request.start iteration=%d messages=%d tools=0 stream=%s",
                         iteration, len(history.view), self.stream)
            with trace.span("llm", iteration,
                            message_count=len(history.view), tool_count=0,
                            messages=list(history.view)) as span_meta:
                response = await chat_with_recovery(
                    self.llm, history.view, None, iteration, harness,
                    stream=self.stream, trace=trace, events=events)
                span_meta.update(tool_count=0, tools=[], final=True)
                logger.debug("llm.request.end iteration=%d content_chars=%d tool_calls=[]",
                             iteration, len(response.get("content") or ""))
        except _ReasoningBudgetExceeded as exc:
            # The forced summary turn has no tools, so nudging makes no
            # sense; surface the partial reasoning as the final answer
            # instead of failing the whole task at the finish line.
            logger.warning("llm.reasoning_budget_exceeded iteration=%d %s",
                           iteration, exc)
            answer = ("(reasoning budget exceeded while summarising; "
                      "the work above stands as delivered)")
            logger.debug("agent.final iteration=%d answer_chars=%d", iteration,
                         len(answer))
            events.emit("agent.completed", iteration, iterations=iteration,
                        answer=answer)
            return answer
        except Exception as exc:
            logger.error("llm.request.error iteration=%d error=%s", iteration, exc)
            error = f"LLM error: {exc}"
            agent_meta.update(stage="llm", error=error)
            raise _TurnFailure(error) from exc
        answer = response.get("content", "")
        self._emit_contract_outcome(answer, iteration, history.messages)
        logger.debug("agent.final iteration=%d answer_chars=%d", iteration, len(answer))
        events.emit("agent.completed", iteration, iterations=iteration, answer=answer)
        return answer

    async def _contract_nudge(self, answer: str, iteration: int,
                                messages: list[dict[str, Any]] | None = None) -> str | None:
        """Enforce the verification return-contract at the finish boundary.

        Returns a retry nudge when the harness requires a structured
        return (labs-OO-Agents TaskResult pattern) and the answer fails
        shape or grounding; ``None`` means the answer may be accepted.
        Grounding compares the answer against the trajectory so far
        (evidence must quote observed output, the declared command must
        have been run, at least one source edit must exist) — generic
        across tasks, no gold tests involved.
        When ``verification.rerun_declared_command`` is true and the
        contract otherwise passes, the declared command is re-executed
        once via the tool layer: exit 0 accepts, anything else rejects
        as a ``command_to_verify`` gap with the rerun output quoted.
        The check costs one iteration of the existing budget.
        """
        verification = self.harness.verification
        if verification.mode != "return_contract":
            return None
        if iteration >= self.max_iterations:
            # No budget left for a retry; accept as-is (outcome recorded
            # on the summary turn instead of forcing an overrun).
            return None
        gaps = check_contract(answer, verification.require, messages)
        if gaps:
            self.trace.emit("verification.failed", iteration,
                            missing=sorted(gaps), reasons=gaps)
            return contract_nudge(gaps)
        if not verification.rerun_declared_command:
            self.trace.emit("verification.passed", iteration,
                            missing=[])
            return None
        return await self._rerun_declared_command(
            extract_field_value(answer, "command_to_verify"), iteration, messages)

    def _take_submit_args(
            self, outcomes: list[ToolCallOutcome]) -> dict[str, Any] | None:
        """First validated submit_result arguments, if this harness honors them.

        Only ``task_result`` mode intercepts the tool; elsewhere a call
        (possible only if the harness enables it) executes as a normal
        tool via its placeholder handler.
        """
        if self.harness.verification.mode != "task_result":
            return None
        for outcome in outcomes:
            if (outcome.validated is not None
                    and outcome.validated.name == SUBMIT_TOOL):
                return dict(outcome.validated.arguments)
        return None

    async def _validate_submission(self, fields: dict[str, Any], iteration: int,
                                   messages: list[dict[str, Any]] | None = None
                                   ) -> str | None:
        """Validate a submit_result finish attempt; ``None`` means accept.

        Renders the typed parameters to the three-section answer shape so
        the submission reuses the free-text gate verbatim, then optionally
        reruns the declared command once.
        """
        verification = self.harness.verification
        gaps = check_contract(render_submission(fields),
                              verification.require, messages)
        if gaps:
            self.trace.emit("verification.failed", iteration,
                            missing=sorted(gaps), reasons=gaps)
            return contract_nudge(gaps)
        if not verification.rerun_declared_command:
            self.trace.emit("verification.passed", iteration,
                            missing=[])
            return None
        raw = fields.get("command_to_verify", "")
        command = raw if isinstance(raw, str) else str(raw)
        return await self._rerun_declared_command(command, iteration, messages)

    async def _rerun_declared_command(self, command: str, iteration: int,
                                      messages: list[dict[str, Any]] | None = None
                                      ) -> str | None:
        """Re-execute the declared verify command once; accept only on exit 0.

        Executes the canonical history command grounding the declaration,
        not the raw declared string: free-text answers may wrap the command
        in fences or trailing prose (v12: 133 exit-2 artifacts), while the
        matched history command is clean and has known exit behavior.
        """
        if not command.strip():
            gaps = {"command_to_verify": "missing or too short"}
            self.trace.emit("verification.failed", iteration,
                            missing=sorted(gaps), reasons=gaps)
            return contract_nudge(gaps)
        target = (match_executed_command(command, executed_commands(messages or []))
                  or command)
        try:
            result = await self.tools.execute("run_command",
                                              {"command": target})
        except Exception as exc:
            gaps = {"command_to_verify":
                    f"rerun failed to execute ({exc}); declare a command "
                    "that runs cleanly before finishing"}
            self.trace.emit("verification.rerun", iteration, command=command,
                            reran=target, success=False, error=str(exc))
            self.trace.emit("verification.failed", iteration,
                            missing=sorted(gaps), reasons=gaps)
            return contract_nudge(gaps)
        success, exit_code, excerpt = _rerun_outcome(result)
        self.trace.emit("verification.rerun", iteration, command=command,
                        reran=target, success=success, exit_code=exit_code,
                        output=excerpt)
        if success:
            self.trace.emit("verification.passed", iteration,
                            missing=[], reran=True)
            return None
        gaps = {"command_to_verify":
                f"rerun exited {exit_code} — fix the failure, re-run it "
                "yourself, then restate the answer"}
        self.trace.emit("verification.failed", iteration,
                        missing=sorted(gaps), reasons=gaps)
        nudge = contract_nudge(gaps)
        if excerpt:
            nudge += f" Rerun output of `{target}`: {excerpt}"
        return nudge

    def _emit_contract_outcome(self, answer: str, iteration: int,
                               messages: list[dict[str, Any]] | None = None) -> None:
        """Record the contract outcome on the budget-exhausted summary turn."""
        if self.harness.verification.mode not in ("return_contract",
                                                  "task_result"):
            return
        gaps = check_contract(answer, self.harness.verification.require, messages)
        self.trace.emit(
            "verification.passed" if not gaps else "verification.failed",
            iteration, missing=sorted(gaps), reasons=gaps)

    def _check_error_budget(self, budget: SameToolErrorBudget,
                              iteration: int, tool: Any,
                              error: Exception) -> str | None:
        """Stop the turn when one tool repeats one failure too often.

        Returns the manual-handling message when the budget trips,
        else ``None`` to let the loop continue.
        """
        count, tripped = budget.note_failure(
            tool if isinstance(tool, str) else None, error)
        if not tripped:
            return None
        detail = str(error).strip().split("\n")[0][:500] or "unknown error"
        answer = (
            f"STOPPED_SAME_TOOL_ERROR: tool {tool!r} failed {count} "
            f"times in a row with the same error: {detail} "
            f"Stopped early to save iterations; "
            f"please handle it manually and resume."
        )
        logger.error("loop.guard iteration=%d tool=%r consecutive=%d error=%s",
                     iteration, tool, count, detail)
        self.trace.emit("loop.guard", iteration, tool=tool,
                        error=str(error)[:500], consecutive=count)
        self.events.emit("agent.completed", iteration,
                         iterations=iteration, answer=answer)
        return answer

    def _reject_tool_call(self, outcome: ToolCallOutcome, iteration: int
                          ) -> dict[str, Any]:
        """Observe a tool call rejected by validation; it never executed.

        Side-effects (logging / trace events) happen exactly once here; the
        returned observation message is the caller's responsibility to append
        via :meth:`TurnHistory.append` (which records it in the history and
        the request view in one step).
        """
        tool_call = outcome.tool_call
        rejection = outcome.rejection
        assert rejection is not None
        payload = _tool_call_payload(tool_call)
        logger.error("tool.error iteration=%d tool=%r error=%s",
                     iteration, payload["tool"], rejection)
        _trace_tool_rejection(self.trace, iteration,
                              _tool_trace_metadata(tool_call), rejection)
        self.events.emit("tool.failed", iteration, call_id=payload["call_id"],
                         tool=payload["tool"], error=str(rejection))
        observation = _rejected_observation_content(
            tool_call,
            self.harness.tool_error_observation(rejection, tool=payload["tool"]))
        return _rejected_observation_message(tool_call, observation)


async def run_turn(user_message: str, llm: ChatModel, tools: ToolExecutor,
               max_iterations: int | None = None, *,
               harness: HarnessSpec | None = None,
               conversation: Conversation | None = None,
               stream: bool = False,
               trace: RunTrace | None = None,
               events: EventEmitter | None = None) -> str:
    """Run one agent turn under the given harness.

    Convenience wrapper around :class:`AgentTurn`. ``max_iterations``
    explicitly overrides ``harness.control``; the harness is otherwise
    the source of truth for how the agent behaves.
    """
    return await AgentTurn(user_message, llm, tools, max_iterations,
                           harness=harness, conversation=conversation,
                           stream=stream, trace=trace, events=events).run()


__all__ = [
    "AgentTurn",
    "ChatModel",
    "Conversation",
    "ToolCallOutcome",
    "ToolExecutor",
    "TurnHistory",
    "ValidatedToolCall",
    "canonical_tool_calls",
    "classify_tool_calls",
    "run_turn",
    "use_streaming",
    "validate_tool_call",
]
