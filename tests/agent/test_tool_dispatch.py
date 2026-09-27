import sys
import tempfile
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from agent_runtime.agent.loop import run_turn
from agent_runtime.agent.tool_dispatch import (
    _available_tools_hint,
    classify_tool_calls,
    validate_tool_call,
)
from agent_runtime.harness import from_dict
from agent_runtime.tools.tools import builtin_tool_specs, create_default_registry
from agent_runtime.execution.local import LocalWorkspace
from agent_runtime.trace import RunTrace


def _schemas() -> list[dict]:
    workspace = LocalWorkspace()
    return [spec.schema for spec in builtin_tool_specs(workspace=workspace)]


def _call(name: str, arguments: str = "{}") -> dict:
    return {"id": "call_1", "type": "function",
            "function": {"name": name, "arguments": arguments}}


class UnknownToolHintTests(unittest.TestCase):
    def test_unknown_tool_lists_available_tools(self) -> None:
        with self.assertRaisesRegex(ValueError, "unknown tool: grep") as caught:
            validate_tool_call(_call("grep"), _schemas())
        message = str(caught.exception)
        for tool in ("run_command", "read_file", "edit_file", "apply_patch",
                     "write_file", "grep_search", "find_files",
                     "submit_result"):
            self.assertIn(tool, message)

    def test_hint_contains_no_guessing(self) -> None:
        hint = _available_tools_hint(_schemas())
        self.assertNotIn("did you mean", hint.lower())
        self.assertNotIn("grep_search", _available_tools_hint([]))

    def test_hint_follows_registry_not_hardcoded_list(self) -> None:
        schemas = _schemas()
        hint = _available_tools_hint(schemas)
        names = [s["function"]["name"] for s in schemas]
        for name in names:
            self.assertIn(name, hint)

    def test_known_tool_still_validates(self) -> None:
        validated = validate_tool_call(
            _call("grep_search", '{"pattern": "x"}'), _schemas())
        self.assertEqual(validated.name, "grep_search")

    def test_classify_marks_unknown_without_raising(self) -> None:
        (outcome,) = classify_tool_calls([_call("bash")], _schemas())
        self.assertIsNone(outcome.validated)
        self.assertIsNotNone(outcome.rejection)
        self.assertIn("Available tools", str(outcome.rejection))


class FakeLLM:
    """Replays one canned assistant turn, then a plain-text finish."""

    def __init__(self, tool_calls: list[dict]) -> None:
        self.tool_calls = tool_calls
        self.turn = 0

    async def chat(self, messages: list[dict],
                   tools: list[dict] | None = None) -> dict:
        self.turn += 1
        if self.turn == 1:
            return {"content": "", "tool_calls": self.tool_calls}
        return {"content": "done", "tool_calls": []}


def _run_one_turn_async(directory: str,
                          tool_calls: list[dict]) -> str:
    """Drive a single agent turn with a real registry rooted at directory."""
    registry = create_default_registry(
        workspace=LocalWorkspace(directory),
        enabled=["write_file", "read_file", "edit_file", "apply_patch"])
    harness = from_dict({"verification": {"mode": "off"}})
    return run_turn("hi", FakeLLM(tool_calls), registry,
                    harness=harness, max_iterations=3,
                    trace=RunTrace(run_id="t"))


class WorkspaceContainmentLoopTests(unittest.IsolatedAsyncioTestCase):
    async def test_escape_write_never_touches_disk(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            answer = await _run_one_turn_async(
                directory, [_call("write_file",
                                  '{"path": "../outside.txt", '
                                  '"content": "pwned"}')])

        self.assertNotIn("pwned", answer)
        self.assertFalse((Path(directory, "outside.txt")).exists())
        self.assertFalse((Path(directory).parent / "outside.txt").exists())

    async def test_typo_path_creates_nothing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            await _run_one_turn_async(
                directory, [_call("write_file",
                                  '{"path": "nope/new.py", '
                                  '"content": "x"}')])

        self.assertFalse((Path(directory) / "nope").exists())


if __name__ == "__main__":
    unittest.main()
