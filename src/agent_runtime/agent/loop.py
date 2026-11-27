"""The minimal tool-calling loop, independent of providers and tools.

Tool calls are validated before they enter the conversation history: only
canonical, validated calls are replayed to the provider, so one malformed
tool call cannot poison later requests.
"""

from __future__ import annotations

import logging
from typing import Any

from agent_runtime.agent.llm_request import (
    ChatModel,
    ThinkingOnlyTurn,
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
from agent_runtime.agent.turn_history import (
    Conversation,
    TurnHistory,
)
from agent_runtime.runtime_config import RuntimeConfig, resolve_echo_reasoning
from agent_runtime.agent.turn_opening import open_turn
from agent_runtime.tools import ToolExecutor

logger = logging.getLogger(__name__)


class TurnFailure(Exception):
    """An expected turn failure whose message becomes the turn's answer.

    Raised and caught only inside this module: the loop turns an LLM
    error into a user-facing answer instead of propagating the exception.
    """


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
        self.guard = collaborators.guard
        self.tool_runner = collaborators.tool_runner
        self.summarizer = collaborators.summarizer
        self.stream = collaborators.stream
        self.max_iterations = collaborators.guard.max_iterations
        self.runtime: RuntimeConfig = collaborators.runtime

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
                  workspace=self.workspace, trace=trace,
                  runtime=self.runtime)
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
                    continue  # turn rewritten (placeholder + nudge); retry next turn
                tool_calls = response.get("tool_calls") or []
                content = response.get("content") or ""

                outcomes = classify_tool_calls(tool_calls, tools.schemas)

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
                    logger.debug("agent.final iteration=%d answer_chars=%d",
                                 iteration, len(content))
                    events.emit("agent.completed", iteration,
                                iterations=iteration, answer=content)
                    agent_meta.update(history.token_totals, iterations=iteration)
                    return content
                stop = await self.tool_runner.dispatch(
                    outcomes, iteration, history)
                if stop is not None:
                    agent_meta.update(history.token_totals, iterations=iteration)
                    return stop

            return await self._summary_turn(history, agent_meta)
        finally:
            agent_meta.update(history.token_totals)

    async def _request(self, history: TurnHistory, iteration: int,
                       schemas: list[dict[str, Any]],
                       agent_meta: dict[str, Any]) -> dict[str, Any] | None:
        """One LLM request; ``None`` means the turn was rewritten (nudged)."""
        events, trace = self.events, self.trace
        harness = self.harness
        try:
            await history.refresh_view(harness.context,
                                       trace=trace, iteration=iteration)
            logger.debug(
                "llm.request.start iteration=%d messages=%d tools=%d stream=%s",
                iteration, len(history.messages), len(schemas), self.stream)
            # ``chat_with_recovery`` owns the per-attempt llm.start/llm.end
            # events (a retry gets its own pair instead of inflating one
            # span), the usage ledger, and every recovery decision
            # (replay / refresh / nudge).  This caller only says which
            # history the request reads from and where to put the result.
            response = await chat_with_recovery(
                self.llm, history, schemas, iteration, harness,
                stream=self.stream, trace=trace, events=events,
                runtime=self.runtime)
            if response is None:
                return None
            tool_calls = response.get("tool_calls") or []
            content = response.get("content") or ""
            logger.debug(
                "llm.request.end iteration=%d content_chars=%d tool_calls=%s",
                iteration, len(content),
                [tool_trace_metadata(call).get("tool") for call in tool_calls])
            return response
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
        ends here.
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
            # ``allow_nudge=False``: the summary turn has no next turn to
            # continue, so a thinking-only response must raise and reach
            # the fallback below instead of rewriting the conversation.
            response = await chat_with_recovery(
                self.llm, history, None, iteration, harness,
                stream=self.stream, trace=trace, events=events,
                allow_nudge=False, runtime=self.runtime)
            # ``allow_nudge=False`` means the nudge strategy re-raises
            # instead of returning ``None``; the only way out of the call
            # with a response is a response.
            assert response is not None
            logger.debug("llm.request.end iteration=%d content_chars=%d tool_calls=[]",
                         iteration, len(response.get("content") or ""))
        except ThinkingOnlyTurn as exc:
            # The forced summary turn has no tools and no next turn, so
            # nudging makes no sense (``allow_nudge=False`` makes the
            # recovery layer raise instead of rewriting); surface the
            # partial work as the final answer instead of failing the
            # whole task at the finish line.
            logger.warning("llm.nudge.unrecoverable iteration=%d kind=%s %s",
                           iteration, exc.kind, exc)
            answer = exc.fallback_answer()
            history.append({"role": "assistant", "content": answer})
            logger.debug("agent.final iteration=%d answer_chars=%d", iteration,
                         len(answer))
            events.emit("agent.completed", iteration, iterations=iteration,
                        answer=answer)
            agent_meta.update(iterations=iteration)
            return answer
        except Exception as exc:
            logger.error("llm.request.error iteration=%d error=%s", iteration, exc)
            error = f"LLM error: {exc}"
            agent_meta.update(stage="llm", error=error)
            raise TurnFailure(error) from exc
        answer = response.get("content", "")
        history.append({"role": "assistant", "content": answer})
        logger.debug("agent.final iteration=%d answer_chars=%d", iteration, len(answer))
        events.emit("agent.completed", iteration, iterations=iteration, answer=answer)
        agent_meta.update(iterations=iteration)
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
