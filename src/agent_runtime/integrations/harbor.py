"""Harbor BaseAgent adapter for SunAgent."""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv
from harbor.agents.base import BaseAgent
from harbor.environments.base import BaseEnvironment
from harbor.models.agent.context import AgentContext

from agent_runtime.agent import run_turn, use_streaming
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
        load_dotenv()
        _enable_runtime_logging()
        self.llm = llm if llm is not None else OpenAICompatibleLLM(model=self.model_name)

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
        use_stream = use_streaming()
        harness: HarnessSpec = resolve_harness(os.getenv("AGENT_RUNTIME_HARNESS"))
        logger.debug("task.start instruction=%r model=%r stream=%s harness=%s",
                     instruction, getattr(self.llm, "model", None),
                     use_stream, harness.id)
        runtime_metadata = self._runtime_metadata(context, instruction, trace_path)

        def sync_context() -> None:
            metadata = dict(context.metadata or {})
            metadata["agent_runtime"] = dict(runtime_metadata)
            context.metadata = metadata

        def record_event(event: RunEvent) -> None:
            runtime_metadata["event_count"] = len(trace.events)
            runtime_metadata["last_event"] = event.to_dict()
            sync_context()

        trace = RunTrace(output_path=trace_path, sink=record_event, harness=harness)
        tools = create_default_registry(HarborShellExecutor(environment),
                                        enabled=list(harness.tools.enabled),
                                        workspace=HarborWorkspace(environment))
        runtime_metadata["status"] = "running"
        sync_context()

        try:
            answer = await run_turn(instruction, self.llm, tools,
                                    harness=harness,
                                    stream=use_stream, trace=trace)
            agent_error = next(
                (event for event in reversed(trace.events)
                 if event.event_type == "agent.error"),
                None,
            )
            runtime_metadata["status"] = "failed" if agent_error else "completed"
            runtime_metadata["answer"] = answer
            logger.debug("task.end status=%s answer=%r", runtime_metadata["status"], answer)
            if agent_error:
                runtime_metadata["error"] = agent_error.data.get("error", answer)
            sync_context()
        except BaseException as exc:
            logger.debug("task.error type=%s message=%s", type(exc).__name__, exc)
            runtime_metadata["status"] = "failed"
            runtime_metadata["error"] = {
                "type": type(exc).__name__,
                "message": str(exc),
            }
            sync_context()
            raise
        finally:
            await trace.flush()

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
