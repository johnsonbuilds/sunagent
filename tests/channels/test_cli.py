import sys
import time
import unittest
from io import StringIO
from pathlib import Path
from typing import Any
from unittest import mock


sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from agent_runtime.channels import CLIRenderer
from agent_runtime.channels import cli as cli_module
from agent_runtime.events import AgentEvent


def event(event_type: str, data: dict[str, Any],
          call_id: str | None = None) -> AgentEvent:
    return AgentEvent("r1", "e1", event_type, 0.0, 1, call_id, data)


def render(events: list[AgentEvent], **kwargs: Any) -> str:
    output = StringIO()
    renderer = CLIRenderer(output, **kwargs)
    for item in events:
        renderer(item)
    return output.getvalue()


class FakeTTY(StringIO):
    def isatty(self) -> bool:
        return True


class CLIRendererTests(unittest.TestCase):
    def test_assistant_deltas_stream_reasoning_then_content(self) -> None:
        output = render([
            event("assistant.delta", {"content": "", "reasoning": "thinking"}),
            event("assistant.delta", {"content": "Hello ", "reasoning": ""}),
            event("assistant.delta", {"content": "world", "reasoning": ""}),
            event("assistant.completed", {"tool_calls": []}),
        ])
        self.assertEqual(output, "▌ thinking\nHello world\n")

    def test_reasoning_can_be_hidden(self) -> None:
        output = render([
            event("assistant.delta", {"content": "", "reasoning": "secret"}),
            event("assistant.delta", {"content": "answer", "reasoning": ""}),
        ], show_reasoning=False)
        self.assertEqual(output, "answer")

    def test_agent_completed_adds_separator(self) -> None:
        output = render([
            event("assistant.delta", {"content": "done", "reasoning": ""}),
            event("agent.completed", {"iterations": 1, "answer": "done"}),
        ])
        self.assertEqual(output, "done\n\n")

    def test_shell_tool_success_renders_command_and_output(self) -> None:
        output = render([
            event("tool.started", {"tool": "run_command",
                                   "arguments": {"command": "pytest"}}, call_id="c1"),
            event("tool.completed", {"exit_code": 0, "duration": 1.8,
                                     "stdout_tail": "test_a ..\ntest_b ..\n2 passed",
                                     "stderr_tail": ""}, call_id="c1"),
        ])
        self.assertEqual(output,
                         "● run_command\n"
                         "  $ pytest\n"
                         "  │ test_a ..\n"
                         "  │ test_b ..\n"
                         "  │ 2 passed\n"
                         "  ✓ exit 0 · 1.8s\n")

    def test_shell_tool_failure_renders_stderr(self) -> None:
        output = render([
            event("tool.started", {"tool": "run_command",
                                   "arguments": {"command": "pytest"}}, call_id="c1"),
            event("tool.completed", {"exit_code": 1, "duration": 2.3,
                                     "stdout_tail": "test_a ..\nF..",
                                     "stderr_tail": "AssertionError: boom"}, call_id="c1"),
        ])
        self.assertEqual(output,
                         "● run_command\n"
                         "  $ pytest\n"
                         "  │ test_a ..\n"
                         "  │ F..\n"
                         "  │ AssertionError: boom\n"
                         "  ✗ exit 1 · 2.3s\n")

    def test_generic_tool_renders_json_arguments_and_result(self) -> None:
        output = render([
            event("tool.started", {"tool": "weather",
                                   "arguments": {"location": "Singapore"}}),
            event("tool.completed", {"result": "Singapore", "duration": 0.2}),
        ])
        self.assertEqual(output,
                         "● weather\n"
                         "  {\"location\": \"Singapore\"}\n"
                         "  │ Singapore\n"
                         "  ✓ done · 0.2s\n")

    def test_execute_code_tool_renders_language_and_first_line(self) -> None:
        output = render([
            event("tool.started", {"tool": "execute_code",
                                  "arguments": {"code": "\nimport os\nprint(os.getcwd())",
                                                "language": "python"}}),
            event("tool.completed", {"exit_code": 0, "duration": 0.5,
                                     "stdout_tail": "/workspace", "stderr_tail": ""}),
        ])
        self.assertEqual(output,
                         "● execute_code\n"
                         "  $ python · import os\n"
                         "  │ /workspace\n"
                         "  ✓ exit 0 · 0.5s\n")

    def test_write_file_tool_renders_path_and_size(self) -> None:
        output = render([
            event("tool.started", {"tool": "write_file",
                                  "arguments": {"path": "a.py", "content": "x = 1\n"}}),
            event("tool.completed", {"result": "{'path': 'a.py', 'bytes_written': 6}",
                                     "duration": 0.1}),
        ])
        self.assertEqual(output,
                         "● write_file\n"
                         "  write a.py (6 chars)\n"
                         "  │ {'path': 'a.py', 'bytes_written': 6}\n"
                         "  ✓ done · 0.1s\n")

    def test_tool_failed_renders_error(self) -> None:
        output = render([
            event("tool.started", {"tool": "weather", "arguments": {"location": 1}}),
            event("tool.failed", {"tool": "weather",
                                  "error": "argument 'location' must be string"}),
        ])
        self.assertEqual(output,
                         "● weather\n"
                         "  {\"location\": 1}\n"
                         "  ✗ argument 'location' must be string\n")

    def test_runtime_error_renders_stage_and_error(self) -> None:
        output = render([
            event("runtime.error", {"stage": "llm", "error": "LLM error: network down"}),
        ])
        self.assertEqual(output, "⚠ llm: LLM error: network down\n\n")

    def test_stdout_tail_is_limited_to_last_lines(self) -> None:
        stdout = "\n".join(f"row {index}" for index in range(10))
        output = render([
            event("tool.completed", {"exit_code": 0, "duration": 1.0,
                                     "stdout_tail": stdout, "stderr_tail": ""}),
        ])
        self.assertIn("│ row 5\n", output)
        self.assertIn("│ row 9\n", output)
        self.assertNotIn("row 4", output)

    def test_unknown_events_are_ignored(self) -> None:
        self.assertEqual(render([event("future.event", {"x": 1})]), "")

    def test_no_color_codes_when_stream_is_not_a_tty(self) -> None:
        output = render([
            event("tool.started", {"tool": "run_command",
                                   "arguments": {"command": "true"}}),
            event("tool.completed", {"exit_code": 0, "duration": 0.1,
                                     "stdout_tail": "", "stderr_tail": ""}),
        ])
        self.assertNotIn("\x1b", output)

    def test_spinner_is_silent_when_stream_is_not_a_tty(self) -> None:
        output = render([
            event("agent.started", {"message": "hi"}),
            event("assistant.started", {}),
            event("assistant.delta", {"content": "Hello", "reasoning": ""}),
        ])
        self.assertEqual(output, "Hello")

    def test_spinner_shows_while_waiting_and_clears_on_first_delta(self) -> None:
        output = FakeTTY()
        renderer = CLIRenderer(output, color=False)
        renderer(event("agent.started", {"message": "hi"}))
        renderer(event("assistant.started", {}))
        waiting = output.getvalue()
        self.assertIn("Thinking", waiting)
        renderer(event("assistant.delta", {"content": "Hello", "reasoning": ""}))
        self.assertTrue(output.getvalue().endswith("\r\x1b[KHello"))
        renderer.close()
        self.assertTrue(output.getvalue().endswith("\r\x1b[KHello"))

    def test_spinner_animates_over_time(self) -> None:
        output = FakeTTY()
        renderer = CLIRenderer(output, color=False)
        with mock.patch.object(cli_module, "_SPINNER_INTERVAL", 0.01):
            renderer(event("assistant.started", {}))
            time.sleep(0.05)
            renderer(event("assistant.completed", {"tool_calls": []}))
        frames = output.getvalue().count("Thinking")
        self.assertGreater(frames, 1)
        renderer.close()

    def test_spinner_stops_on_tool_and_restarts_for_next_llm_round(self) -> None:
        output = FakeTTY()
        renderer = CLIRenderer(output, color=False)
        renderer(event("assistant.started", {}))
        self.assertIn("Thinking", output.getvalue())
        renderer(event("tool.started", {"tool": "run_command",
                                        "arguments": {"command": "ls"}}))
        tool_output = output.getvalue()
        self.assertTrue(tool_output.endswith("  $ ls\n"))
        renderer(event("assistant.started", {}))
        self.assertIn("Thinking", output.getvalue()[len(tool_output):])
        renderer(event("assistant.completed", {"tool_calls": []}))
        renderer.close()

    def test_spinner_can_be_disabled_with_env_var(self) -> None:
        output = FakeTTY()
        with mock.patch.dict("os.environ", {"AGENT_RUNTIME_CLI_SPINNER": "0"}):
            renderer = CLIRenderer(output, color=False)
        renderer(event("assistant.started", {}))
        self.assertEqual(output.getvalue(), "")
        renderer.close()


if __name__ == "__main__":
    unittest.main()
