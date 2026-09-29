import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any


sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from agent_runtime.agent.loop import run_turn
from agent_runtime.execution.local import LocalWorkspace
from agent_runtime.trace import RunTrace
from agent_runtime.tools.tools import create_default_registry


CODE_TOOLS = ["execute_code", "read_file"]


def make_registry(directory: str):
    return create_default_registry(workspace=LocalWorkspace(directory),
                                   enabled=CODE_TOOLS)


class FakeLLM:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self.responses = list(responses)
        self.messages: list[list[dict[str, Any]]] = []

    async def chat(self, messages: list[dict[str, Any]],
                   tools: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        self.messages.append(messages.copy())
        return self.responses.pop(0)


class ExecuteCodeTests(unittest.IsolatedAsyncioTestCase):
    async def test_runs_python_script_and_saves_it(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = make_registry(directory)

            result = await registry.execute("execute_code", {
                "code": "print('hello')\n", "language": "python"})
            saved = await registry.execute("read_file", {"path": ".scripts/0001.py"})

        self.assertEqual(result["stdout"], "hello\n")
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["script_path"], ".scripts/0001.py")
        self.assertEqual(result["language"], "python")
        self.assertEqual(saved["content"], "print('hello')\n")

    async def test_sequence_numbers_advance_across_languages(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = make_registry(directory)

            first = await registry.execute("execute_code", {
                "code": "print('py')\n"})
            second = await registry.execute("execute_code", {
                "code": "echo bash\n", "language": "bash"})

        self.assertEqual(first["script_path"], ".scripts/0001.py")
        self.assertEqual(second["script_path"], ".scripts/0002.sh")
        self.assertEqual(second["stdout"], "bash\n")

    async def test_explicit_path_is_used(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = make_registry(directory)

            result = await registry.execute("execute_code", {
                "code": "print('x')\n", "path": ".scripts/custom.py"})
            saved = await registry.execute("read_file", {"path": ".scripts/custom.py"})

        self.assertEqual(result["script_path"], ".scripts/custom.py")
        self.assertEqual(saved["content"], "print('x')\n")

    async def test_explicit_path_outside_scripts_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = make_registry(directory)

            result = await registry.execute("execute_code", {
                "code": "print('x')\n", "path": "analysis/run.py"})

        self.assertEqual(result["error"]["type"], "InvalidPath")
        self.assertIn(".scripts/", result["error"]["message"])

    async def test_python_footgun_import_is_blocked(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = make_registry(directory)

            result = await registry.execute("execute_code", {
                "code": "import subprocess\nsubprocess.run(['ls'])\n"})

        self.assertEqual(result["error"]["type"], "CommandBlocked")
        self.assertIn("footgun", result["error"]["message"])

    async def test_concurrent_calls_get_unique_names(self) -> None:
        import asyncio

        with tempfile.TemporaryDirectory() as directory:
            registry = make_registry(directory)

            results = await asyncio.gather(*[
                registry.execute("execute_code", {"code": f"print({i})\n"})
                for i in range(5)
            ])

        paths = sorted(r["script_path"] for r in results)
        self.assertEqual(len(set(paths)), 5)
        self.assertEqual(paths[0], ".scripts/0001.py")

    async def test_failing_script_returns_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = make_registry(directory)

            result = await registry.execute("execute_code", {
                "code": "raise ValueError('boom')\n"})

        self.assertNotEqual(result["exit_code"], 0)
        self.assertIn("ValueError: boom", result["stderr"])
        self.assertEqual(result["script_path"], ".scripts/0001.py")

    async def test_unsupported_language_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = make_registry(directory)

            result = await registry.execute("execute_code", {
                "code": "print('x')\n", "language": "cobol"})

        self.assertEqual(result["error"]["type"], "UnsupportedLanguage")
        self.assertIn("unsupported language", result["error"]["message"])

    async def test_oversized_stdout_is_capped_at_source(self) -> None:
        # The executor caps each stream at MAX_OUTPUT_CHARS so a runaway
        # print cannot OOM the process; the transcript boundary still
        # spills what remains to .outputs/.
        from agent_runtime.execution.base import MAX_OUTPUT_CHARS

        with tempfile.TemporaryDirectory() as directory:
            registry = make_registry(directory)

            result = await registry.execute("execute_code", {
                "code": f"print('x' * {MAX_OUTPUT_CHARS + 5000})\n"})

        self.assertTrue(result.get("truncated"))
        self.assertLessEqual(len(result["stdout"]), MAX_OUTPUT_CHARS + 100)
        self.assertIn("output truncated", result["stdout"])

    async def test_r_language_runs_via_rscript(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = make_registry(directory)

            result = await registry.execute("execute_code", {
                "code": "cat('hello from R\n')\n", "language": "r"})

        self.assertEqual(result["script_path"], ".scripts/0001.R")
        self.assertEqual(result["language"], "r")
        if result["exit_code"] == 0:
            self.assertIn("hello from R", result["stdout"])
        else:
            self.assertEqual(result["exit_code"], 127)
            self.assertIn("'Rscript' was not found", result["stderr"])
            self.assertIn("install R", result["stderr"])

    async def test_node_language_runs_via_node(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = make_registry(directory)

            result = await registry.execute("execute_code", {
                "code": "console.log('hello from node')\n", "language": "node"})

        self.assertEqual(result["script_path"], ".scripts/0001.js")
        self.assertEqual(result["language"], "node")
        if result["exit_code"] == 0:
            self.assertIn("hello from node", result["stdout"])
        else:
            self.assertEqual(result["exit_code"], 127)
            self.assertIn("'node' was not found", result["stderr"])

    async def test_missing_interpreter_gets_install_hint(self) -> None:
        from agent_runtime.tools.code import _interpreter_for, _with_interpreter_hint

        result = {"stdout": "", "stderr": "/bin/sh: 1: rscript: not found",
                  "exit_code": 127, "duration": 0.1}
        hinted = _with_interpreter_hint(result, "r")

        self.assertEqual(hinted["exit_code"], 127)
        self.assertIn("'Rscript' was not found", hinted["stderr"])
        self.assertIn("r-base-core", hinted["stderr"])
        self.assertIn("run_command", hinted["stderr"])
        # Original fields are untouched and the input dict is not mutated.
        self.assertIn("rscript: not found", hinted["stderr"])
        self.assertNotIn("hint:", result["stderr"])

    async def test_non_127_failure_gets_no_hint(self) -> None:
        from agent_runtime.tools.code import _with_interpreter_hint

        result = {"stdout": "", "stderr": "ValueError: boom", "exit_code": 1,
                  "duration": 0.1}
        self.assertIs(_with_interpreter_hint(result, "python"), result)

    async def test_oversized_stdout_is_returned_raw_for_boundary_spill(self) -> None:
        # Truncation/spilling moved to the transcript boundary
        # (agent_runtime.agent.observations); the tool returns raw results.
        with tempfile.TemporaryDirectory() as directory:
            registry = make_registry(directory)

            result = await registry.execute("execute_code", {
                "code": "print('x' * 20000)\n"})

        self.assertEqual(result["stdout"], "x" * 20000 + "\n")
        self.assertNotIn("stdout_chars", result)
        self.assertNotIn("stdout_full_path", result)

    async def test_run_turn_feeds_one_observation_back_to_the_llm(self) -> None:
        llm = FakeLLM([
            {"content": "", "tool_calls": [{"id": "1", "function": {
                "name": "execute_code",
                "arguments": '{"code": "print(21 * 2)"}'}}]},
            {"content": "42", "tool_calls": []},
        ])

        with tempfile.TemporaryDirectory() as directory:
            registry = make_registry(directory)
            answer = await run_turn("double 21", llm, registry,
                                    trace=RunTrace(run_id="code"))

            saved = await registry.execute("read_file", {"path": ".scripts/0001.py"})

        self.assertEqual(answer, "42")
        observation = llm.messages[1][-1]
        self.assertEqual(observation["role"], "tool")
        self.assertEqual(observation["tool_call_id"], "1")
        self.assertIn("42", observation["content"])
        self.assertIn(".scripts/0001.py", observation["content"])
        self.assertEqual(saved["content"], "print(21 * 2)")


if __name__ == "__main__":
    unittest.main()
