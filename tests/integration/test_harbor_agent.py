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

from agent_runtime.integrations.harbor import HarborAgent, _with_repo_context


class FakeShell:
    def __init__(self, stdout: str = "", fail: bool = False) -> None:
        self.stdout = stdout
        self.fail = fail
        self.commands: list[str] = []

    async def execute(self, command: str, timeout: float | None = None) -> dict[str, Any]:
        self.commands.append(command)
        if self.fail:
            raise RuntimeError("container unreachable")
        return {"stdout": self.stdout, "stderr": "", "exit_code": 0}


class RepoContextTests(unittest.IsolatedAsyncioTestCase):
    async def test_probe_result_is_appended(self) -> None:
        shell = FakeShell("[git-log]\nabc1234 fix widgets\n"
                          "[python]\nPython 3.11.2\n"
                          "[pytest]\npytest 8.3.1\n")
        instruction = await _with_repo_context("do the task", shell)

        self.assertIn("<repo_context>", instruction)
        self.assertIn("abc1234 fix widgets", instruction)
        self.assertIn("pytest 8.3.1", instruction)
        self.assertEqual(len(shell.commands), 1)

    async def test_empty_probe_leaves_instruction_untouched(self) -> None:
        shell = FakeShell("[git-log]\n[python]\n[pytest]\n")
        instruction = await _with_repo_context("do the task", shell)

        self.assertEqual(instruction, "do the task")

    async def test_exec_failure_leaves_instruction_untouched(self) -> None:
        shell = FakeShell(fail=True)
        instruction = await _with_repo_context("do the task", shell)

        self.assertEqual(instruction, "do the task")


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
            # One repo-context probe (single exec, empty result here so the
            # instruction is unchanged) followed by the model's command.
            self.assertEqual(len(environment.commands), 2)
            self.assertIn("[git-log]", environment.commands[0][0])
            self.assertEqual(environment.commands[1], ("printf hello", None, 30))


if __name__ == "__main__":
    unittest.main()
