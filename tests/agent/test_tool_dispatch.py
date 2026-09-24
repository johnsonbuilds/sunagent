import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from agent_runtime.agent.tool_dispatch import (
    _available_tools_hint,
    classify_tool_calls,
    validate_tool_call,
)
from agent_runtime.tools.tools import builtin_tool_specs
from agent_runtime.execution.local import LocalWorkspace


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


if __name__ == "__main__":
    unittest.main()
