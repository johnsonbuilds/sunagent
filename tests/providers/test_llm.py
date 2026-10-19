import os
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from agent_runtime.providers.llm import (
    OpenAICompatibleLLM,
    _project_messages_for_wire,
    _reasoning_echo_mode,
    _should_echo_reasoning,
)


class ReasoningEchoModeTests(unittest.TestCase):
    def test_default_is_auto(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AGENT_RUNTIME_REASONING_ECHO", None)
            self.assertEqual(_reasoning_echo_mode(), "auto")

    def test_values_are_case_insensitive(self) -> None:
        for raw, expected in (("auto", "auto"), ("Always", "always"),
                              ("NEVER", "never")):
            with patch.dict(os.environ, {"AGENT_RUNTIME_REASONING_ECHO": raw}):
                self.assertEqual(_reasoning_echo_mode(), expected)

    def test_unknown_value_falls_back_to_auto(self) -> None:
        with patch.dict(os.environ, {"AGENT_RUNTIME_REASONING_ECHO": "sometimes"}):
            self.assertEqual(_reasoning_echo_mode(), "auto")


class ShouldEchoReasoningTests(unittest.TestCase):
    def test_always_and_never_pin_the_direction(self) -> None:
        self.assertTrue(_should_echo_reasoning("gpt-4o", None, "always"))
        self.assertFalse(_should_echo_reasoning("deepseek-v4", None, "never"))

    def test_auto_detects_echo_families_by_model(self) -> None:
        for model in ("deepseek/deepseek-v4.1-flash", "glm-5.3",
                      "kimi-k2", "MiMo-7B", "ling-flash"):
            self.assertTrue(_should_echo_reasoning(model, "https://api.example.com/v1",
                                                   "auto"), model)

    def test_auto_detects_echo_families_by_base_url(self) -> None:
        self.assertTrue(_should_echo_reasoning(
            "custom-model", "https://api.moonshot.cn/v1", "auto"))
        self.assertTrue(_should_echo_reasoning(
            "custom-model", "https://open.bigmodel.cn/api/paas/v4", "auto"))

    def test_auto_strips_for_everyone_else(self) -> None:
        self.assertFalse(_should_echo_reasoning("gpt-4o",
                                                "https://api.openai.com/v1", "auto"))
        self.assertFalse(_should_echo_reasoning("mistral-large", None, "auto"))

    def test_reads_mode_from_env_when_not_pinned(self) -> None:
        with patch.dict(os.environ, {"AGENT_RUNTIME_REASONING_ECHO": "never"}):
            self.assertFalse(_should_echo_reasoning("deepseek-v4", None))
        with patch.dict(os.environ, {"AGENT_RUNTIME_REASONING_ECHO": "always"}):
            self.assertTrue(_should_echo_reasoning("gpt-4o", None))


class ProjectMessagesForWireTests(unittest.TestCase):
    def _history(self) -> list[dict[str, Any]]:
        return [
            {"role": "system", "content": "sys"},
            {"role": "assistant", "content": "visible",
             "reasoning_content": "inner monologue"},
            {"role": "assistant", "content": "",
             "reasoning_content": "thinking only",
             "tool_calls": [{"id": "c1", "type": "function",
                             "function": {"name": "run_command",
                                          "arguments": "{}"}}]},
            {"role": "tool", "tool_call_id": "c1", "content": "out",
             "metadata": {"spill_path": ".outputs/obs-1.txt"}},
        ]

    def test_echo_keeps_reasoning_strips_metadata(self) -> None:
        projected = _project_messages_for_wire(self._history(), echo=True)

        self.assertEqual(projected[1]["reasoning_content"], "inner monologue")
        self.assertEqual(projected[2]["reasoning_content"], "thinking only")
        self.assertNotIn("metadata", projected[3])
        self.assertEqual(projected[3]["content"], "out")

    def test_no_echo_strips_reasoning(self) -> None:
        projected = _project_messages_for_wire(self._history(), echo=False)

        self.assertNotIn("reasoning_content", projected[1])
        self.assertEqual(projected[1]["content"], "visible")
        self.assertNotIn("reasoning_content", projected[2])

    def test_thinking_only_turn_gets_placeholder(self) -> None:
        history = [
            {"role": "assistant", "content": "", "reasoning_content": "spiral"},
        ]

        projected = _project_messages_for_wire(history, echo=False)

        self.assertEqual(projected[0]["content"], "(reasoning omitted)")
        self.assertNotIn("reasoning_content", projected[0])

    def test_tool_calling_turn_is_not_padded(self) -> None:
        projected = _project_messages_for_wire(self._history(), echo=False)

        self.assertEqual(projected[2]["content"], "")
        self.assertTrue(projected[2]["tool_calls"])

    def test_projection_does_not_mutate_history(self) -> None:
        history = self._history()
        snapshot = [dict(m) for m in history]

        _project_messages_for_wire(history, echo=False)

        self.assertEqual(history, snapshot)
        self.assertEqual(history[1]["reasoning_content"], "inner monologue")
        self.assertEqual(history[3]["metadata"],
                         {"spill_path": ".outputs/obs-1.txt"})


class _FakeMessage:
    def __init__(self) -> None:
        self.content = "ok"
        self.tool_calls: list[Any] = []
        self.reasoning_content = None


class _FakeCompletions:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if kwargs.get("stream"):
            async def _chunks() -> Any:
                yield {"content": "ok", "reasoning_content": "",
                       "tool_calls": [],
                       "finish_reason": "stop"} if False else _Chunk("ok", "stop")
            return _chunks()
        response = unittest.mock.MagicMock()
        response.choices = [unittest.mock.MagicMock(message=_FakeMessage())]
        return response


class _Chunk:
    def __init__(self, content: str, finish_reason: str | None = None) -> None:
        delta = unittest.mock.MagicMock()
        delta.content = content
        delta.reasoning_content = ""
        delta.tool_calls = []
        self.choices = [unittest.mock.MagicMock(delta=delta,
                                                finish_reason=finish_reason)]


class ProviderEgressTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self) -> None:
        self.completions = _FakeCompletions()
        self.llm = OpenAICompatibleLLM(api_key="test-key",
                                       base_url="https://api.example.com/v1",
                                       model="deepseek-v4")
        self.llm.client = unittest.mock.MagicMock()
        self.llm.client.chat.completions = self.completions

    async def test_chat_projects_messages_for_the_wire(self) -> None:
        history = [
            {"role": "assistant", "content": "hi",
             "reasoning_content": "thinking",
             "metadata": {"spill_path": "x"}},
        ]

        await self.llm.chat(history)

        sent = self.completions.calls[0]["messages"]
        self.assertEqual(sent[0]["reasoning_content"], "thinking")
        self.assertNotIn("metadata", sent[0])
        # History stays intact for the next iteration and the trace.
        self.assertEqual(history[0]["reasoning_content"], "thinking")
        self.assertIn("metadata", history[0])

    async def test_stream_projects_messages_for_the_wire(self) -> None:
        with patch.dict(os.environ, {"AGENT_RUNTIME_REASONING_ECHO": "never"}):
            self.llm.model = "gpt-4o"
            history = [
                {"role": "assistant", "content": "",
                 "reasoning_content": "spiral"},
            ]

            chunks = [chunk async for chunk in self.llm.stream(history)]

        sent = self.completions.calls[0]["messages"]
        self.assertNotIn("reasoning_content", sent[0])
        self.assertEqual(sent[0]["content"], "(reasoning omitted)")
        self.assertEqual(chunks[-1].get("finish_reason"), "stop")


if __name__ == "__main__":
    unittest.main()
