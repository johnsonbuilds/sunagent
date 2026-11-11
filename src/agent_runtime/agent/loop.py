"""The minimal tool-calling loop, independent of providers and tools.

Tool calls are validated before they enter the conversation history: only
canonical, validated calls are replayed to the provider, so one malformed
tool call cannot poison later requests.
"""

from __future__ import annotations

import logging
from typing import Any

from agent_runtime.agent.chat import (
    ChatModel,
    ReasoningBudgetExceeded,
    chat_with_recovery,
    use_streaming,
)
from agent_runtime.agent.runtime_message import runtime_user_message
from agent_runtime.agent.tool_dispatch import (
    ToolCallOutcome,
    ValidatedToolCall,
    canonical_tool_calls,
    classify_tool_calls,
    validate_tool_call,
    tool_trace_metadata,
)
from agent_runtime.agent.turn_collaborators import TurnCollaborators
from agent_runtime.agent.turn_failure import TurnFailure
from agent_runtime.agent.turn_history import (
    Conversation,
    TurnHistory,
)
from agent_runtime.agent.tokens import response_usage
from agent_runtime.runtime_config import RuntimeConfig, resolve_echo_reasoning
from agent_runtime.agent.turn_opening import open_turn
from agent_runtime.tools import ToolExecutor

logger = logging.getLogger(__name__)


class AgentTurn:
    """One agent turn: collaborators and policy bound as attributes.

    Instances are assembled by
    :func:`agent_runtime.agent.turn_factory.create_agent_turn`; ``run``
    then drives the tool-calling loop, and helpers read ``self`` instead
    of threading a dozen parameters through every call.
    """

    def __init__(self, user_message: str,
                 collaborators: TurnCollaborators) -> None:
        self.user_message = user_message
        self.collaborators = collaborators
        self.llm = collaborators.llm
        self.tools = collaborators.tools
        self.workspace = collaborators.workspace
        self.harness = collaborators.harness
        self.conversation = collaborators.conversation
        self.trace = collaborators.trace
        self.events = collaborators.events
        self.finisher = collaborators.finisher
        self.guard = collaborators.guard
        self.tool_runner = collaborators.tool_runner
        self.summarizer = collaborators.summarizer
        self.stream = collaborators.stream
        self.max_iterations = collaborators.max_iterations
        self.skills_dir = collaborators.skills_dir
        self.runtime: RuntimeConfig | None = collaborators.runtime

    async def run(self) -> str:
        """Execute the turn and always flush the trace."""
        agent_meta: dict[str, Any] = {}
        try:
            with self.trace.span("agent") as agent_meta:
                return await self._run(agent_meta)
        except TurnFailure as exc:
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
        open_turn(conversation=self.conversation, harness=harness,
                  workspace=self.workspace, skills_dir=self.skills_dir,
                  trace=trace)
        self.conversation.append({"role": "user", "content": self.user_message})
        history = TurnHistory(self.conversation.messages,
                              summarizer=self.summarizer,
                              count_reasoning=resolve_echo_reasoning(
                                  self.llm, self.runtime),
                              runtime=self.runtime)

        try:
            for iteration in range(1, self.max_iterations + 1):
                self.guard.maybe_remind(history, iteration)
                response = await self._request(history, iteration, tools.schemas,
                                               agent_meta)
                if response is None:
                    continue  # reasoning-budget nudge appended; retry next turn
                tool_calls = response.get("tool_calls") or []
                content = response.get("content") or ""

                outcomes = classify_tool_calls(tool_calls, tools.schemas)
                # A submit call is a finish attempt, not work: other
                # calls in the same turn still execute first, then the first
                # submission is validated against the updated history.
                submit_args = self.finisher.take_submit_arguments(outcomes)

                assistant_message: dict[str, Any] = {
                    "role": "assistant",
                    "content": content,
                }
                reasoning_content = response.get("reasoning_content")
                if reasoning_content:
                    assistant_message["reasoning_content"] = reasoning_content
                # A submit call is a finish request, not work: it is
                # never executed (see ToolRunner), so it must not enter
                # the history as a tool call — otherwise the next request
                # carries an assistant tool_calls entry with no following
                # tool message and the provider rejects it with 400
                # ("must be followed by tool messages").
                canonical_calls = canonical_tool_calls(outcomes)
                if submit_args is not None:
                    submit_tool = self.finisher.rules.submit_tool
                    submit_ids = {outcome.validated.id for outcome in outcomes
                                  if outcome.validated is not None
                                  and outcome.validated.name == submit_tool}
                    canonical_calls = [call for call in canonical_calls
                                       if call["id"] not in submit_ids]
                if canonical_calls:
                    assistant_message["tool_calls"] = canonical_calls
                if tool_calls or content or reasoning_content:
                    history.append(assistant_message)

                if not tool_calls:
                    finished = self.finisher.answer_without_tools(response,
                                                                  iteration, history)
                    if finished is not None:
                        agent_meta.update(history.token_totals)
                        return finished
                    continue
                stop = await self.tool_runner.dispatch(
                    outcomes, submit_args, iteration, history)
                if stop is not None:
                    agent_meta.update(history.token_totals)
                    return stop

                if submit_args is not None:
                    finished = await self.finisher.finish_or_nudge(
                        submit_args, iteration, history)
                    if finished is not None:
                        agent_meta.update(history.token_totals)
                        return finished
                    continue

            return await self._summary_turn(history, agent_meta)
        finally:
            agent_meta.update(history.token_totals)

    async def _request(self, history: TurnHistory, iteration: int,
                       schemas: list[dict[str, Any]],
                       agent_meta: dict[str, Any]) -> dict[str, Any] | None:
        """One LLM request; ``None`` means a reasoning-budget nudge was added."""
        events, trace = self.events, self.trace
        harness = self.harness
        try:
            await history.refresh_view(harness.context,
                                       trace=trace, iteration=iteration)
            logger.debug(
                "llm.request.start iteration=%d messages=%d tools=%d stream=%s",
                iteration, len(history.messages), len(schemas), self.stream)
            with trace.span("llm", iteration,
                            message_count=len(history.view),
                            tool_count=len(schemas),
                            messages=list(history.view)) as span_meta:
                response = await chat_with_recovery(
                    self.llm, history.view, schemas, iteration,
                    harness, stream=self.stream, trace=trace, events=events,
                    refresh=self.guard.context_window_refresh(
                        history, iteration),
                    runtime=self.runtime)
                tool_calls = response.get("tool_calls") or []
                content = response.get("content") or ""
                prompt_tokens, completion_tokens = response_usage(response)
                history.record_usage(prompt_tokens, completion_tokens)

                span_meta.update(
                    tool_count=len(tool_calls),
                    tools=[tool_trace_metadata(call).get("tool")
                           for call in tool_calls],
                    final=not tool_calls,
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=prompt_tokens + completion_tokens,
                )
                logger.debug(
                    "llm.request.end iteration=%d content_chars=%d tool_calls=%s",
                    iteration, len(content),
                    [tool_trace_metadata(call).get("tool") for call in tool_calls])
                return response
        except ReasoningBudgetExceeded as exc:
            # The model spiralled into thinking without acting. Append
            # an assistant placeholder plus a nudge and retry on the
            # next iteration (consuming one iteration of the budget) —
            # resending identical input would just reproduce the spiral.
            logger.warning(
                "llm.reasoning_budget_exceeded iteration=%d %s", iteration, exc)
            history.append({"role": "assistant",
                            "content": "(extended reasoning omitted)"})
            nudge = runtime_user_message(
                f"Your last turn produced {exc.reasoning_chars} characters "
                f"of reasoning without any content or tool calls. Stop "
                f"extending your reasoning. Produce the smallest deliverable "
                f"that makes progress now: write the minimal artifact, or "
                f"call the tool that creates it. Do not keep planning, and "
                f"do not give a final answer unless the task's required "
                f"result already exists.",
                kind="reasoning_budget",
            )
            history.append(nudge)
            return None
        except Exception as exc:
            logger.error("llm.request.error iteration=%d error=%s", iteration, exc)
            error = f"LLM error: {exc}"
            agent_meta.update(stage="llm", error=error)
            agent_meta.update(history.token_totals)
            raise TurnFailure(error) from exc

    async def _summary_turn(self, history: TurnHistory,
                            agent_meta: dict[str, Any]) -> str:
        """Forced summary turn once the iteration budget is spent.

        Appends the harness iteration-limit notice and asks one last time
        with no tools, so the model can only summarise; the turn always
        ends here, with the contract recorded as a miss.
        """
        events, trace = self.events, self.trace
        harness = self.harness
        history.append(runtime_user_message(
            harness.prompt.iteration_limit_notice, kind="iteration_limit"))
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
                    refresh=self.guard.context_window_refresh(
                        history, iteration),
                    runtime=self.runtime)
                prompt_tokens, completion_tokens = response_usage(response)
                history.record_usage(prompt_tokens, completion_tokens)
                span_meta.update(tool_count=0, tools=[], final=True,
                                 prompt_tokens=prompt_tokens,
                                 completion_tokens=completion_tokens,
                                 total_tokens=prompt_tokens + completion_tokens)
                logger.debug("llm.request.end iteration=%d content_chars=%d tool_calls=[]",
                             iteration, len(response.get("content") or ""))
        except ReasoningBudgetExceeded as exc:
            # The forced summary turn has no tools, so nudging makes no
            # sense; surface the partial reasoning as the final answer
            # instead of failing the whole task at the finish line.
            logger.warning("llm.reasoning_budget_exceeded iteration=%d %s",
                           iteration, exc)
            answer = ("(reasoning budget exceeded while summarising; "
                      "the work above stands as delivered)")
            history.append({"role": "assistant", "content": answer})
            logger.debug("agent.final iteration=%d answer_chars=%d", iteration,
                         len(answer))
            events.emit("agent.completed", iteration, iterations=iteration,
                        answer=answer)
            return answer
        except Exception as exc:
            logger.error("llm.request.error iteration=%d error=%s", iteration, exc)
            error = f"LLM error: {exc}"
            agent_meta.update(stage="llm", error=error)
            raise TurnFailure(error) from exc
        answer = response.get("content", "")
        history.append({"role": "assistant", "content": answer})
        self.finisher.emit_contract_outcome(iteration)
        logger.debug("agent.final iteration=%d answer_chars=%d", iteration, len(answer))
        events.emit("agent.completed", iteration, iterations=iteration, answer=answer)
        return answer


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
    "use_streaming",
    "validate_tool_call",
]
