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


# One exec that answers the environment questions models otherwise spend
# iterations probing (git log, python/pytest versions). Best-effort and
# self-gating: each section is included only when the probe returned
# something, so non-git / non-python tasks (e.g. terminal-bench) simply get
# a shorter context instead of noise. Never raises; trial startup must not
# depend on it.
_REPO_CONTEXT_COMMAND = (
    "echo [git-log]; git log --oneline -5 2>/dev/null; "
    "echo [python]; python -V 2>&1; "
    "echo [pytest]; python -m pytest --version 2>&1 | head -1"
)
_REPO_CONTEXT_BUDGET = 800


async def _with_repo_context(instruction: str, shell: HarborShellExecutor) -> str:
    """Append observed container facts to the task instruction, if any."""
    try:
        outcome = await shell.execute(_REPO_CONTEXT_COMMAND, timeout=30.0)
    except Exception:
        return instruction
    if not isinstance(outcome, dict) or outcome.get("error"):
        return instruction
    sections: list[str] = []
    current: list[str] = []
    label = ""
    for line in str(outcome.get("stdout", "")).splitlines():
        stripped = line.strip()
        if stripped in ("[git-log]", "[python]", "[pytest]"):
            if label and any(ln.strip() for ln in current):
                sections.append(f"{label}:\n" + "\n".join(current).strip())
            label = stripped.strip("[]")
            current = []
        else:
            current.append(line)
    if label and any(ln.strip() for ln in current):
        sections.append(f"{label}:\n" + "\n".join(current).strip())
    if not sections:
        return instruction
    context = "\n".join(sections)[:_REPO_CONTEXT_BUDGET]
    return (
        f"{instruction}\n\n<repo_context>\n"
        f"Observed in the container (for reference only):\n{context}\n"
        "</repo_context>"
    )


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
        shell = HarborShellExecutor(environment)
        tools = create_default_registry(shell,
                                        enabled=list(harness.tools.enabled),
                                        workspace=HarborWorkspace(environment))
        instruction = await _with_repo_context(instruction, shell)
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
