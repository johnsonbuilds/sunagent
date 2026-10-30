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
    check_submission,
    contract_nudge,
    has_source_edit,
    render_submission,
    submit_nudge,
)
from agent_runtime.agent.verification_rerun import (
    _RERUN_TIMEOUT,
    rerun_declaration_broken,
    rerun_failure_detail,
    rerun_outcome,
)
from agent_runtime.agent.observations import ObservationFormatter
from agent_runtime.agent.context import request_view_size
from agent_runtime.agent.turn_history import (
    Conversation,
    TurnHistory,
    _ensure_system_prompt,
)
from agent_runtime.events import EventEmitter
from agent_runtime.harness import HarnessSpec, default_harness
from agent_runtime.memory import render_memory_block
from agent_runtime.skills import (
    render_skills_block,
    resolve_skills,
    skills_trace_meta,
)
from agent_runtime.tools import ToolExecutor
from agent_runtime.trace import RunTrace

logger = logging.getLogger(__name__)

# Finish-signal tool honored only under a task_result verification
# harness; elsewhere it is inert registry data.
SUBMIT_TOOL = "submit_result"


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
                 events: EventEmitter | None = None,
                 skills_dir: Any | None = None) -> None:
        self.harness = harness or default_harness()
        self.skills_dir = skills_dir
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
        # from context.head_chars so the invariant is structural.
        self.observations = ObservationFormatter(
            getattr(self.tools, "workspace", None),
            max_chars=self.harness.control.max_observation_chars,
            spill_preview_chars=self.harness.control.spill_preview_chars,
            archive_min_chars=self.harness.context.head_chars)
        self.user_message = user_message
        self.llm = llm
        self.conversation = conversation or Conversation()
        self.stream = stream
        # Consecutive finish attempts rejected without interleaving work
        # (plain-text finishes, invalid submissions). At the limit the
        # turn aborts instead of burning the iteration budget on prose.
        self._finish_violations = 0

    def _context_window_refresh(self, history: TurnHistory,
                                iteration: int):
        """Build the hard-gate refresh hook handed to chat recovery.

        On a context-window rejection the hook shrinks the working
        budget to 70% of the rejected request's estimated size, rebuilds
        the view, and copies the new content into the list reference the
        recovery loop already holds (mutating in place, not rebinding).
        Returns False when the strategy cannot shrink further (identity
        or no size reduction — resending would be rejected again), which
        lets the recovery loop re-raise instead of burning retries.
        """
        async def _refresh() -> bool:
            current = request_view_size(history.view, self.harness.context,
                                        history.usage_anchor)
            anchor_before = history.usage_anchor
            target = max(1, int(current * 0.7))
            view_ref = history.view
            try:
                await history.refresh_view(self.harness.context,
                                           trace=self.trace,
                                           iteration=iteration,
                                           force_budget=target)
            except Exception as exc:
                logger.warning(
                    "llm.context_window.refresh_failed iteration=%d %s",
                    iteration, exc)
                return False
            if history.view is view_ref:
                return False
            # Compare with the pre-refresh anchor: refresh clears it
            # (stale mix), and measuring old vs new with different rulers
            # would misjudge shrinkage.
            if request_view_size(history.view, self.harness.context,
                                 anchor_before) >= current:
                logger.warning(
                    "llm.context_window.no_shrink iteration=%d", iteration)
                return False
            view_ref[:] = history.view
            return True
        return _refresh

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
        skills = resolve_skills(list(harness.skills),
                                skills_dir=self.skills_dir,
                                harness=harness)
        skills_block = render_skills_block(skills)
        memory_view = render_memory_block(getattr(tools, "workspace", None))
        inserted = _ensure_system_prompt(self.conversation, harness.prompt.system,
                              skills_block, memory_view.block)
        # Both blocks ride the single resident system message: only trace
        # what the model will actually see, so later turns in the same
        # conversation (no-op injection) emit neither event.
        if inserted:
            if skills:
                trace.emit("skills.loaded", 0, **skills_trace_meta(skills))
            if memory_view.block:
                trace.emit("memory.recall", 0, **memory_view.trace_meta())
        self.conversation.append({"role": "user", "content": self.user_message})
        history = TurnHistory(self.conversation.messages)
        error_budget = SameToolErrorBudget(
            resolve_max_retries(harness.recovery.tool_error_max_retries))

        for iteration in range(1, self.max_iterations + 1):
            self._maybe_emit_budget_reminder(history, iteration)
            try:
                await history.refresh_view(harness.context,
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
                        harness, stream=self.stream, trace=trace, events=events,
                        refresh=self._context_window_refresh(
                            history, iteration))
                    tool_calls = response.get("tool_calls") or []
                    content = response.get("content") or ""
                    usage = response.get("usage")
                    if usage and int(usage.get("prompt_tokens") or 0) > 0:
                        history.record_usage(int(usage["prompt_tokens"]))

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
            # A submit_result call is a finish request, not work: it is
            # never executed (see _dispatch_tools), so it must not enter
            # the history as a tool call — otherwise the next request
            # carries an assistant tool_calls entry with no following
            # tool message and the provider rejects it with 400
            # ("must be followed by tool messages").
            canonical_calls = canonical_tool_calls(outcomes)
            if submit_args is not None:
                submit_ids = {outcome.validated.id for outcome in outcomes
                              if outcome.validated is not None
                              and outcome.validated.name == SUBMIT_TOOL}
                canonical_calls = [call for call in canonical_calls
                                   if call["id"] not in submit_ids]
            if canonical_calls:
                assistant_message["tool_calls"] = canonical_calls
            if tool_calls or content or reasoning_content:
                history.append(assistant_message)

            if not tool_calls:
                finished = self._answer_without_tools(response, iteration,
                                                      history)
                if finished is not None:
                    return finished
                continue
            stop = await self._dispatch_tools(outcomes, submit_args, iteration,
                                              history, error_budget)
            if stop is not None:
                return stop

            if submit_args is not None:
                finished = await self._finish_or_nudge(submit_args, iteration,
                                                       history)
                if finished is not None:
                    return finished
                continue

        return await self._summary_turn(history, agent_meta)

    def _answer_without_tools(self, response: dict[str, Any], iteration: int,
                              history: TurnHistory) -> str | None:
        """Handle an answer with no tool calls; ``None`` means nudge + continue.

        Under ``verification.mode == "off"`` the plain answer ends the turn;
        an enabled harness only accepts a typed submit_result call, so a bare
        message is either the budget-exhausted fallback or a contract miss
        worth one nudge.
        """
        events = self.events
        answer = response.get("content", "")
        if self.harness.verification.mode == "off":
            logger.debug("agent.final iteration=%d answer_chars=%d",
                         iteration, len(answer))
            events.emit("agent.completed", iteration, iterations=iteration,
                        answer=answer)
            return answer
        # Labs-OO-Agents BenchAgent pattern: a bare message can
        # never finish the turn — the model must submit via the
        # submit_result tool, whose parameters are typed. Legacy
        # return_contract prompts describe text sections, but the
        # runtime no longer accepts them either.
        if iteration >= self.max_iterations:
            # No budget left for a retry; accept as-is and record
            # the miss on the summary turn instead of overrunning.
            self.trace.emit("verification.failed", iteration,
                            missing=["submit_result"],
                            reasons={"submit_result":
                                     "budget exhausted without a valid "
                                     "submit_result call"})
            logger.debug("agent.final iteration=%d answer_chars=%d "
                         "unsubmitted", iteration, len(answer))
            events.emit("agent.completed", iteration, iterations=iteration,
                        answer=answer)
            return answer
        self._note_finish_violation(iteration)
        gaps = {"submit_result": "plain-text answer cannot finish "
                                 "the task — call submit_result"}
        self.trace.emit("verification.failed", iteration,
                        missing=sorted(gaps), reasons=gaps)
        history.append({"role": "user", "content": submit_nudge()})
        return None

    async def _dispatch_tools(self, outcomes: list[ToolCallOutcome],
                              submit_args: dict[str, Any] | None, iteration: int,
                              history: TurnHistory,
                              error_budget: SameToolErrorBudget) -> str | None:
        """Execute this iteration's tool calls in order; ``None`` lets it on.

        A validated submit_result call is skipped here — it is a finish
        attempt, validated against the updated history afterwards. A
        returned string is a guard stop (same-tool error budget) for the
        caller to return as the turn's answer.
        """
        events, trace = self.events, self.trace
        tools, harness = self.tools, self.harness
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
                if payload["tool"] == SUBMIT_TOOL:
                    # Malformed finish attempt, not work: no reset.
                    self._note_finish_violation(iteration)
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
                self._finish_violations = 0  # executed: new information
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
            self._finish_violations = 0  # executed work resets the fuse
        return None

    async def _finish_or_nudge(self, submit_args: dict[str, Any], iteration: int,
                               history: TurnHistory) -> str | None:
        """Validate the turn's submit_result attempt; ``None`` means nudge.

        The nudge is appended to the history and the loop continues; a
        rendered submission is the final answer of the turn.
        """
        events = self.events
        nudge = await self._validate_submission(
            submit_args, iteration, history.messages)
        if nudge is not None:
            history.append({"role": "user", "content": nudge})
            return None
        answer = render_submission(submit_args)
        logger.debug("agent.final iteration=%d answer_chars=%d submit=True",
                     iteration, len(answer))
        events.emit("agent.completed", iteration, iterations=iteration,
                    answer=answer)
        return answer

    async def _summary_turn(self, history: TurnHistory,
                            agent_meta: dict[str, Any]) -> str:
        """Forced summary turn once the iteration budget is spent.

        Appends the harness iteration-limit notice and asks one last time
        with no tools, so the model can only summarise; the turn always
        ends here, with the contract recorded as a miss.
        """
        events, trace = self.events, self.trace
        harness = self.harness
        history.append({"role": "user", "content":
                        harness.prompt.iteration_limit_notice})
        iteration = self.max_iterations + 1
        try:
            await history.refresh_view(harness.context,
                                       trace=trace, iteration=iteration)
            logger.debug("llm.request.start iteration=%d messages=%d tools=0 stream=%s",
                         iteration, len(history.view), self.stream)
            with trace.span("llm", iteration,
                            message_count=len(history.view), tool_count=0,
                            messages=list(history.view)) as span_meta:
                response = await chat_with_recovery(
                    self.llm, history.view, None, iteration, harness,
                    stream=self.stream, trace=trace, events=events,
                    refresh=self._context_window_refresh(
                        history, iteration))
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

    def _maybe_emit_budget_reminder(self, history: TurnHistory, iteration: int) -> None:
        """Close-loop nudge at configured budget fractions (control gene).

        Fires once per threshold: reports remaining budget + whether a
        source edit exists, and forces the file-level test + submit path.
        Detection reuses verification helpers — no new state tracked.
        """
        reminder = self.harness.control.budget_reminder
        if not reminder.enabled:
            return
        if iteration < 2:
            return
        triggered = any(
            int(f * self.max_iterations) == iteration for f in reminder.at_fractions
        )
        if not triggered:
            return
        edited = has_source_edit(history.messages)
        remaining = self.max_iterations - iteration
        history.append({
            "role": "user",
            "content": (
                f"Budget {iteration}/{self.max_iterations} "
                f"({remaining} left). Source edit so far: {edited}. "
                "Close the loop: 1) make >=1 source edit with the right tool "
                "(edit_file default single-point, apply_patch only for atomic "
                "multi-hunk, write_file new/full rewrite), "
                "2) run a FILE-LEVEL test command naming test files, "
                "3) submit_result with that exact command."
            ),
        })
        self.trace.emit("budget.reminder", iteration,
                        remaining=remaining, edited=edited)

    def _note_finish_violation(self, iteration: int) -> None:
        """Count a rejected finish attempt; abort at the harness limit.

        Plain-text finishes and invalid submissions burn budget without
        progress, so consecutive violations (no executed tool call in
        between) abort the turn instead — the labs-OO-Agents
        ``max_consecutive_text_only`` pattern. ``0`` disables the fuse.
        """
        limit = self.harness.control.finish_violation_limit
        if limit <= 0:
            return
        self._finish_violations += 1
        if self._finish_violations >= limit:
            self.trace.emit("verification.aborted", iteration,
                            consecutive=self._finish_violations)
            raise _TurnFailure(
                f"ABORTED_REPEATED_FINISH_VIOLATIONS: {self._finish_violations} "
                f"consecutive finish attempts rejected without interleaving work "
                f"(limit {limit}). Finish by calling submit_result with valid "
                 f"parameters after running the tests.")

    def _take_submit_args(
            self, outcomes: list[ToolCallOutcome]) -> dict[str, Any] | None:
        """First validated submit_result arguments, if this harness honors them.

        Any enabled verification mode intercepts the tool; ``off`` lets a
        call through to the placeholder handler.
        """
        if self.harness.verification.mode == "off":
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

        Checks shape plus grounding on the typed parameters, then always
        re-executes a declared command once. Non-code harnesses (no
        ``command_to_verify`` declared) skip the rerun: there is nothing
        to confirm.
        """
        verification = self.harness.verification
        gaps = check_submission(fields, verification.require, messages)
        if gaps:
            self._note_finish_violation(iteration)
            self.trace.emit("verification.failed", iteration,
                            missing=sorted(gaps), reasons=gaps)
            return contract_nudge(gaps)
        raw = fields.get("command_to_verify", "")
        command = raw if isinstance(raw, str) else str(raw)
        if not command.strip():
            self.trace.emit("verification.passed", iteration,
                            missing=[])
            return None
        return await self._rerun_declared_command(command, iteration)

    async def _rerun_declared_command(self, command: str,
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
            return contract_nudge(gaps)
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
            return contract_nudge(gaps)
        success, exit_code, excerpt = rerun_outcome(result)
        self.trace.emit("verification.rerun", iteration, command=command,
                        success=success, exit_code=exit_code, output=excerpt)
        if success:
            self.trace.emit("verification.passed", iteration,
                            missing=[], reran=True)
            return None
        if rerun_declaration_broken(exit_code, excerpt):
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
        detail = rerun_failure_detail(excerpt)
        # Result + reason + acceptance criterion only: no fix target
        # (tests vs. source belongs to the task instructions) and no
        # prescribed action (re-running is wasteful when only the
        # declaration is wrong) — the model decides the next step.
        gaps = {"command_to_verify":
                f"rerun of your declared command exited {exit_code}{detail}; "
                "a submission is only accepted when that command exits 0"}
        self.trace.emit("verification.failed", iteration,
                        missing=sorted(gaps), reasons=gaps)
        nudge = contract_nudge(gaps)
        if excerpt:
            nudge += f" Rerun output of `{command}`: {excerpt}"
        return nudge

    def _emit_contract_outcome(self, answer: str, iteration: int,
                               messages: list[dict[str, Any]] | None = None) -> None:
        """Record the contract outcome on the budget-exhausted summary turn.

        The summary turn carries no tools, so a submission is impossible:
        the outcome is always a miss. ``messages`` is kept for signature
        symmetry with the finish path.
        """
        if self.harness.verification.mode == "off":
            return
        del answer, messages
        self.trace.emit("verification.failed", iteration,
                        missing=["submit_result"],
                        reasons={"submit_result":
                                 "budget exhausted without a valid "
                                 "submit_result call"})

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
               events: EventEmitter | None = None,
               skills_dir: Any | None = None) -> str:
    """Run one agent turn under the given harness.

    Convenience wrapper around :class:`AgentTurn`. ``max_iterations``
    explicitly overrides ``harness.control``; the harness is otherwise
    the source of truth for how the agent behaves.
    """
    return await AgentTurn(user_message, llm, tools, max_iterations,
                           harness=harness, conversation=conversation,
                           stream=stream, trace=trace, events=events,
                           skills_dir=skills_dir).run()


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
