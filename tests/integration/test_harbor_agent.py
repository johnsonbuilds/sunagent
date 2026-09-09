import sys
import types
import unittest
import logging
from contextlib import redirect_stderr
from io import StringIO
from os import environ
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any


sys.path.insert(0, str(Path(__file__).parents[2] / "src"))


class FakeBaseAgent:
    def __init__(self, logs_dir: Path, model_name: str | None = None, **kwargs: Any) -> None:
        self.logs_dir = logs_dir
        self.model_name = model_name


class FakeBaseEnvironment:
    def __init__(self) -> None:
        self.commands: list[tuple[str, str | None, int | None]] = []

    async def exec(self, command: str, cwd: str | None = None,
                   timeout_sec: int | None = None) -> Any:
        self.commands.append((command, cwd, timeout_sec))
        return types.SimpleNamespace(stdout="hello", stderr="", return_code=0)


class FakeAgentContext:
    def __init__(self, metadata: dict[str, Any] | None = None) -> None:
        self.metadata = metadata


def install_harbor_stubs() -> None:
    modules = {
        "harbor": types.ModuleType("harbor"),
        "harbor.agents": types.ModuleType("harbor.agents"),
        "harbor.agents.base": types.ModuleType("harbor.agents.base"),
        "harbor.environments": types.ModuleType("harbor.environments"),
        "harbor.environments.base": types.ModuleType("harbor.environments.base"),
        "harbor.models": types.ModuleType("harbor.models"),
        "harbor.models.agent": types.ModuleType("harbor.models.agent"),
        "harbor.models.agent.context": types.ModuleType("harbor.models.agent.context"),
    }
    modules["harbor.agents.base"].BaseAgent = FakeBaseAgent
    modules["harbor.environments.base"].BaseEnvironment = FakeBaseEnvironment
    modules["harbor.models.agent.context"].AgentContext = FakeAgentContext
    sys.modules.update(modules)


install_harbor_stubs()

from agent_runtime.integrations.harbor import HarborAgent


class FakeLLM:
    def __init__(self) -> None:
        self.responses = [
            {"content": "", "tool_calls": [{"id": "call-1", "function": {
                "name": "run_command", "arguments": '{"command":"printf hello"}'}}]},
            {"content": "completed", "tool_calls": []},
        ]

    async def chat(self, messages: list[dict[str, Any]], tools: Any = None) -> dict[str, Any]:
        return self.responses.pop(0)

    async def stream(self, messages: list[dict[str, Any]], tools: Any = None):
        yield self.responses.pop(0)
        yield {"content": "", "reasoning_content": "", "finish_reason": "stop"}


class HarborAgentSmokeTests(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_logging_can_be_enabled_for_task_and_stream(self) -> None:
        output = StringIO()
        previous = environ.get("AGENT_RUNTIME_LOG_STREAM")
        environ["AGENT_RUNTIME_LOG_STREAM"] = "1"
        try:
            with redirect_stderr(output), TemporaryDirectory() as directory:
                agent = HarborAgent(Path(directory), model_name="test-model", llm=FakeLLM())
                await agent.run("inspect task", FakeBaseEnvironment(), FakeAgentContext())
        finally:
            if previous is None:
                environ.pop("AGENT_RUNTIME_LOG_STREAM", None)
            else:
                environ["AGENT_RUNTIME_LOG_STREAM"] = previous
            runtime_logger = logging.getLogger("agent_runtime")
            for handler in runtime_logger.handlers[:]:
                runtime_logger.removeHandler(handler)
                handler.close()
            runtime_logger.propagate = True
            runtime_logger.setLevel(logging.NOTSET)

        self.assertIn("task.start", output.getvalue())
        # Per-chunk llm.chunk logging is intentionally disabled (blank
        # deltas used to flood stderr); the streaming lifecycle lines
        # below still prove the streaming path is observed.
        self.assertIn("llm.stream.start", output.getvalue())
        self.assertIn("llm.stream.end", output.getvalue())

    async def test_run_reuses_runtime_and_populates_context(self) -> None:
        with TemporaryDirectory() as directory:
            agent = HarborAgent(Path(directory), model_name="test-model", llm=FakeLLM())
            context = FakeAgentContext()
            environment = FakeBaseEnvironment()

            await agent.run("create a file", environment, context)

            runtime = context.metadata["agent_runtime"]
            self.assertEqual(runtime["status"], "completed")
            self.assertEqual(runtime["answer"], "completed")
            self.assertGreater(runtime["event_count"], 0)
            self.assertTrue(Path(runtime["trace_path"]).exists())
            self.assertEqual(environment.commands, [("printf hello", None, 30)])


if __name__ == "__main__":
    unittest.main()
