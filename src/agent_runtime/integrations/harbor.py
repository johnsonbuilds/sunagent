"""Harbor BaseAgent adapter for SunAgent."""

from __future__ import annotations

import asyncio
import logging
import os
from dataclasses import replace
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from harbor.agents.base import BaseAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from agent_runtime.agent import run_turn
from agent_runtime.metrics import afinalize_run
from agent_runtime.runtime_config import RuntimeConfig, resolve_echo_reasoning
from agent_runtime.execution.harbor import HarborShellExecutor, HarborWorkspace
from agent_runtime.harness import HarnessSpec, resolve_harness
from agent_runtime.providers import OpenAICompatibleLLM
from agent_runtime.tools import create_default_registry
from agent_runtime.trace import RunEvent, RunTrace


logger = logging.getLogger(__name__)


def _enable_runtime_logging() -> None:
    """Enable a small stderr logger without changing Harbor's logging setup."""
    if os.getenv("AGENT_RUNTIME_LOG_STREAM", "").lower() not in {"1", "true", "yes"}:
        return
    runtime_logger = logging.getLogger("agent_runtime")
    runtime_logger.setLevel(logging.DEBUG)
    if not runtime_logger.handlers:
        handler = logging.StreamHandler()
        handler.setFormatter(logging.Formatter("[agent-runtime] %(message)s"))
        runtime_logger.addHandler(handler)
    runtime_logger.propagate = False


class HarborAgent(BaseAgent):
    """Thin Harbor adapter that delegates execution to the existing runtime."""

    SUPPORTS_WINDOWS = False

    def __init__(self, *args: Any, llm: Any | None = None, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        load_dotenv(override=True)
        # Harbor tasks must never receive persistent memory: the memory
        # tree is host-local state, and injecting it would leak context
        # across tasks.  Disabling it on the config (not on
        # ``os.environ``) keeps the choice scoped to this agent instead
        # of mutating global process state for the host application.
        self._runtime = replace(RuntimeConfig.from_env(), memory_enabled=False)
        _enable_runtime_logging()
        self.llm = llm if llm is not None else OpenAICompatibleLLM(model=self.model_name, config=self._runtime)

    @staticmethod
    def name() -> str:
        return "sunagent"

    def version(self) -> str:
        return "0.1.0"

    async def setup(self, environment: BaseEnvironment) -> None:
        """The runtime needs no environment setup before the first turn."""

    async def run(
        self,
        instruction: str,
        environment: BaseEnvironment,
        context: AgentContext,
    ) -> None:
        trace_path = self.logs_dir / "agent-runtime.jsonl"
        harness: HarnessSpec = resolve_harness(self._runtime.harness)
        use_stream = self._runtime.streaming if isinstance(getattr(self, "_runtime", None), RuntimeConfig) else RuntimeConfig.from_env().streaming
        logger.debug("task.start instruction=%r model=%r stream=%s harness=%s",
                     instruction, getattr(self.llm, "model", None),
                     use_stream, harness.id)
        runtime_metadata = self._runtime_metadata(context, instruction, trace_path)

        def sync_context() -> None:
            metadata = dict(context.metadata or {})
            metadata["agent_runtime"] = dict(runtime_metadata)
            context.metadata = metadata

        def record_event(event: RunEvent) -> None:
            # ``trace.events`` is a control-only window: count emitted
            # events, and keep a small payload-free view of the last one.
            runtime_metadata["event_count"] = trace.event_count
            runtime_metadata["last_event"] = {
                "event_type": event.event_type,
                "timestamp": event.timestamp,
                "iteration": event.iteration,
            }
            sync_context()

        trace = RunTrace(output_path=trace_path, sink=record_event,
                         harness=harness, retain_events="control",
                         origin="harbor", model=getattr(self.llm, "model", "") or "")
        tools = create_default_registry(HarborShellExecutor(environment),
                                        enabled=list(harness.tools.enabled),
                                        workspace=HarborWorkspace(environment))
        trace.record_config(
            runtime=getattr(self, "_runtime", None),
            model_params={"model": getattr(self.llm, "model", ""),
                          "temperature": self._runtime.temperature,
                          "stream": use_stream,
                          "max_iterations": harness.control.max_iterations},
            tools=tools.schemas,
            # The adapter may override the model (see __init__), so resolve
            # from the llm itself; re-deriving from runtime can record a
            # different bit than the wire uses.
            reasoning_echo=resolve_echo_reasoning(self.llm),
        )
        runtime_metadata["status"] = "running"
        sync_context()
        status, answer, failure = "error", "", None

        try:
            answer = await run_turn(instruction, self.llm, tools,
                                    harness=harness,
                                    stream=use_stream, trace=trace,
                                    runtime=getattr(self, "_runtime", None))
            agent_error = trace.agent_error()
            status = "error" if agent_error else "completed"
            runtime_metadata["status"] = "failed" if agent_error else "completed"
            runtime_metadata["answer"] = answer
            logger.debug("task.end status=%s answer=%r", runtime_metadata["status"], answer)
            if agent_error:
                runtime_metadata["error"] = agent_error.data.get("error", answer)
                failure = {"stage": "agent",
                           "code": str(agent_error.data.get("error") or answer)}
            sync_context()
        except BaseException as exc:
            logger.debug("task.error type=%s message=%s", type(exc).__name__, exc)
            runtime_metadata["status"] = "failed"
            runtime_metadata["error"] = {
                "type": type(exc).__name__,
                "message": str(exc),
            }
            status = "cancelled" if isinstance(exc, asyncio.CancelledError) else "error"
            failure = {"stage": "runtime", "code": type(exc).__name__}
            sync_context()
            raise
        finally:
            # One call owns the whole close: it flushes the turn's trace,
            # emits ``run.metrics`` and flushes the writer that event
            # restarts.  A bare ``flush`` + ``finalize_run`` would leave
            # that writer parked on the queue forever.
            await afinalize_run(trace, status=status, answer=answer,
                                error=failure, origin="harbor",
                                model=getattr(self.llm, "model", "") or "")

    @staticmethod
    def _runtime_metadata(
        context: AgentContext,
        instruction: str,
        trace_path: Path,
    ) -> dict[str, Any]:
        metadata = dict(context.metadata or {})
        runtime_metadata = dict(metadata.get("agent_runtime") or {})
        runtime_metadata.update({
            "instruction": instruction,
            "trace_path": str(trace_path),
            "event_count": 0,
        })
        metadata["agent_runtime"] = runtime_metadata
        context.metadata = metadata
        return runtime_metadata


__all__ = ["HarborAgent"]
