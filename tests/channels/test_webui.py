import sys
import unittest
from pathlib import Path
from typing import Any


sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from agent_runtime.channels import WebUIChannel
from agent_runtime.channels import webui as webui_module
from agent_runtime.events import AgentEvent


def event(event_type: str, data: dict[str, Any] | None = None,
          call_id: str | None = None, run_id: str = "r1",
          iteration: int = 1) -> AgentEvent:
    return AgentEvent(run_id, "e1", event_type, 0.0, iteration, call_id,
                      data or {})


def frames(events: list[AgentEvent], **kwargs: Any) -> list[dict[str, Any]]:
    collected: list[dict[str, Any]] = []
    channel = WebUIChannel(sink=collected.append, **kwargs)
    for item in events:
        channel(item)
    return collected


def by_type(collected: list[dict[str, Any]], frame_type: str) -> list[dict[str, Any]]:
    return [frame for frame in collected if frame.get("type") == frame_type]


class WebUIChannelTests(unittest.TestCase):
    def test_agent_started_emits_a_run_frame(self) -> None:
        collected = frames([event("agent.started", {"message": "hi"})])
        self.assertEqual(len(collected), 1)
        self.assertEqual(collected[0]["type"], "run.started")
        self.assertEqual(collected[0]["run_id"], "r1")
        self.assertEqual(collected[0]["message"], "hi")

    def test_assistant_deltas_stream_into_one_block(self) -> None:
        collected = frames([
            event("agent.started"),
            event("assistant.started"),
            event("assistant.delta", {"content": "Hel"}),
            event("assistant.delta", {"content": "lo"}),
        ])
        deltas = by_type(collected, "delta")
        self.assertEqual([delta["text"] for delta in deltas], ["Hel", "lo"])
        self.assertEqual(deltas[0]["id"], deltas[1]["id"])
        self.assertEqual(deltas[0]["channel"], "assistant")

    def test_reasoning_and_content_use_separate_blocks(self) -> None:
        collected = frames([
            event("agent.started"),
            event("assistant.delta", {"content": "answer", "reasoning": "why"}),
        ])
        deltas = by_type(collected, "delta")
        self.assertEqual([delta["channel"] for delta in deltas],
                         ["reasoning", "assistant"])
        self.assertNotEqual(deltas[0]["id"], deltas[1]["id"])

    def test_reasoning_can_be_disabled(self) -> None:
        collected = frames([
            event("agent.started"),
            event("assistant.delta", {"content": "answer", "reasoning": "why"}),
            event("assistant.completed"),
        ], show_reasoning=False)
        deltas = by_type(collected, "delta")
        self.assertEqual([delta["channel"] for delta in deltas], ["assistant"])
        self.assertEqual(by_type(collected, "segment")[0]["segment"]["content"],
                         "answer")

    def test_reasoning_follows_the_environment_default(self) -> None:
        with unittest.mock.patch.dict("os.environ",
                                      {"AGENT_RUNTIME_WEBUI_REASONING": "0"}):
            collected = frames([
                event("agent.started"),
                event("assistant.delta", {"reasoning": "why"}),
            ])
        self.assertEqual(by_type(collected, "delta"), [])

    def test_assistant_completed_finalizes_the_block(self) -> None:
        collected = frames([
            event("agent.started"),
            event("assistant.delta", {"content": "done"}),
            event("assistant.completed", {"tool_calls": []}),
        ])
        segments = by_type(collected, "segment")
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0]["segment"]["type"], "assistant")
        self.assertEqual(segments[0]["segment"]["content"], "done")
        self.assertEqual(segments[0]["segment"]["id"],
                         by_type(collected, "delta")[0]["id"])

    def test_tool_started_summarises_the_command(self) -> None:
        collected = frames([
            event("agent.started"),
            event("tool.started", {"tool": "run_command",
                                   "arguments": {"command": "pytest"}},
                  call_id="c1"),
        ])
        started = by_type(collected, "tool.started")
        self.assertEqual(len(started), 1)
        self.assertEqual(started[0]["summary"], "$ pytest")
        self.assertEqual(started[0]["call_id"], "c1")
        self.assertEqual(started[0]["arguments"], {"command": "pytest"})

    def test_tool_completed_reports_a_shell_result(self) -> None:
        collected = frames([
            event("agent.started"),
            event("tool.started", {"tool": "run_command",
                                   "arguments": {"command": "pytest"}},
                  call_id="c1"),
            event("tool.completed", {"exit_code": 0, "duration": 1.25,
                                     "stdout_tail": "2 passed",
                                     "stderr_tail": ""}, call_id="c1"),
        ])
        completed = by_type(collected, "tool.completed")
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0]["status"], "ok")
        self.assertEqual(completed[0]["output"], "2 passed")
        self.assertEqual(completed[0]["duration"], 1.25)
        self.assertEqual(completed[0]["tool"], "run_command")
        segment = by_type(collected, "segment")[0]["segment"]
        self.assertEqual(segment["id"], completed[0]["id"])

    def test_failed_shell_command_is_marked_as_an_error(self) -> None:
        collected = frames([
            event("agent.started"),
            event("tool.started", {"tool": "run_command",
                                   "arguments": {"command": "false"}},
                  call_id="c1"),
            event("tool.completed", {"exit_code": 1, "duration": 0.1,
                                     "stdout_tail": "",
                                     "stderr_tail": "boom"}, call_id="c1"),
        ])
        completed = by_type(collected, "tool.completed")[0]
        self.assertEqual(completed["status"], "error")
        self.assertEqual(completed["exit_code"], 1)
        self.assertIn("boom", completed["output"])

    def test_tool_failed_carries_the_error(self) -> None:
        collected = frames([
            event("agent.started"),
            event("tool.failed", {"tool": "write_file", "error": "denied"},
                  call_id="c1"),
        ])
        failed = by_type(collected, "tool.failed")
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["error"], "denied")
        self.assertEqual(failed[0]["status"], "error")

    def test_generic_tool_result_is_carried_through(self) -> None:
        collected = frames([
            event("agent.started"),
            event("tool.started", {"tool": "find_files",
                                   "arguments": {"pattern": "*.py"}},
                  call_id="c1"),
            event("tool.completed", {"duration": 0.2, "result": "a.py"},
                  call_id="c1"),
        ])
        completed = by_type(collected, "tool.completed")[0]
        self.assertEqual(completed["output"], "a.py")
        self.assertEqual(completed["status"], "ok")

    def test_agent_completed_reports_the_answer(self) -> None:
        collected = frames([
            event("agent.started"),
            event("agent.completed", {"iterations": 2, "answer": "42"}),
        ])
        completed = by_type(collected, "run.completed")
        self.assertEqual(len(completed), 1)
        self.assertEqual(completed[0]["answer"], "42")
        self.assertEqual(completed[0]["iterations"], 2)

    def test_runtime_error_is_forwarded(self) -> None:
        collected = frames([
            event("agent.started"),
            event("runtime.error", {"stage": "llm", "error": "boom"}),
        ])
        failed = by_type(collected, "run.error")
        self.assertEqual(len(failed), 1)
        self.assertEqual(failed[0]["stage"], "llm")
        self.assertEqual(failed[0]["error"], "boom")

    def test_unknown_events_are_ignored(self) -> None:
        collected = frames([
            event("agent.started"),
            event("something.new", {"value": 1}),
        ])
        self.assertEqual(len(collected), 1)

    def test_close_flushes_an_unfinished_block(self) -> None:
        collected: list[dict[str, Any]] = []
        channel = WebUIChannel(sink=collected.append)
        channel(event("agent.started"))
        channel(event("assistant.delta", {"content": "partial"}))
        channel.close()
        segments = by_type(collected, "segment")
        self.assertEqual(len(segments), 1)
        self.assertEqual(segments[0]["segment"]["content"], "partial")

    def test_segments_are_collected_in_transcript_order(self) -> None:
        collected: list[dict[str, Any]] = []
        channel = WebUIChannel(sink=collected.append)
        channel(event("agent.started"))
        channel(event("assistant.delta", {"content": "text"}))
        channel(event("assistant.completed"))
        channel(event("tool.started", {"tool": "run_command",
                                       "arguments": {"command": "ls"}},
                      call_id="c1"))
        channel(event("tool.completed", {"exit_code": 0, "duration": 0.1,
                                         "stdout_tail": "", "stderr_tail": ""},
                      call_id="c1"))
        self.assertEqual([segment["type"] for segment in channel.segments],
                         ["assistant", "tool"])


class DescribeToolTests(unittest.TestCase):
    def test_known_tools_get_a_short_summary(self) -> None:
        cases = [
            ("run_command", {"command": "ls -la"}, "$ ls -la"),
            ("write_file", {"path": "a.py", "content": "x"}, "write a.py (1 chars)"),
            ("grep_search", {"pattern": "todo"}, "grep todo"),
            ("find_symbol", {"name": "main"}, "symbol main"),
        ]
        for tool, arguments, expected in cases:
            self.assertEqual(webui_module.describe_tool(tool, arguments),
                             expected)

    def test_unknown_tools_fall_back_to_json(self) -> None:
        summary = webui_module.describe_tool("mystery", {"a": 1})
        self.assertEqual(summary, '{"a": 1}')


if __name__ == "__main__":
    unittest.main()
