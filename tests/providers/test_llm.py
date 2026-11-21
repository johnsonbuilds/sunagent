import json
import os
import sys
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
from openai.types.chat.chat_completion_chunk import (
    ChoiceDeltaToolCall,
    ChoiceDeltaToolCallFunction,
)
from openai.types.chat.chat_completion_message_tool_call import (
    ChatCompletionMessageToolCall,
    Function,
)


sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from agent_runtime.agent.runtime_message import runtime_user_message
from agent_runtime.providers.llm import (
    OpenAICompatibleLLM,
    _project_messages_for_wire,
    _reasoning_text,
    _usage_dict,
)
from agent_runtime.reasoning_echo import (
    reasoning_echo_mode,
    should_echo_reasoning,
)


class ProviderUsageTests(unittest.TestCase):
    """The provider adapter must not drop the prompt-cache count."""

    def test_object_usage_with_openai_details(self) -> None:
        from types import SimpleNamespace

        usage = SimpleNamespace(prompt_tokens=100, completion_tokens=20,
                                prompt_tokens_details=SimpleNamespace(
                                    cached_tokens=64))

        self.assertEqual(_usage_dict(usage)["cached_tokens"], 64)

    def test_dict_usage_with_deepseek_cache_field(self) -> None:
        usage = {"prompt_tokens": 10, "completion_tokens": 2,
                 "prompt_cache_hit_tokens": 7}

        self.assertEqual(_usage_dict(usage)["cached_tokens"], 7)

    def test_flat_cached_tokens_wins(self) -> None:
        usage = {"prompt_tokens": 10, "completion_tokens": 2,
                 "cached_tokens": 3, "prompt_cache_hit_tokens": 7}

        self.assertEqual(_usage_dict(usage)["cached_tokens"], 3)

    def test_missing_usage_is_zero(self) -> None:
        self.assertEqual(_usage_dict(None)["cached_tokens"], 0)
        self.assertEqual(_usage_dict(None)["prompt_tokens"], 0)


class ReasoningEchoModeTests(unittest.TestCase):
    def test_default_is_auto(self) -> None:
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AGENT_RUNTIME_REASONING_ECHO", None)
            self.assertEqual(reasoning_echo_mode(), "auto")

    def test_values_are_case_insensitive(self) -> None:
        for raw, expected in (("auto", "auto"), ("Always", "always"),
                              ("NEVER", "never")):
            with patch.dict(os.environ, {"AGENT_RUNTIME_REASONING_ECHO": raw}):
                self.assertEqual(reasoning_echo_mode(), expected)

    def test_unknown_value_falls_back_to_auto(self) -> None:
        with patch.dict(os.environ, {"AGENT_RUNTIME_REASONING_ECHO": "sometimes"}):
            self.assertEqual(reasoning_echo_mode(), "auto")


class ShouldEchoReasoningTests(unittest.TestCase):
    def test_always_and_never_pin_the_direction(self) -> None:
        self.assertTrue(should_echo_reasoning("gpt-4o", None, "always"))
        self.assertFalse(should_echo_reasoning("deepseek-v4", None, "never"))

    def test_auto_detects_echo_families_by_model(self) -> None:
        for model in ("deepseek/deepseek-v4.1-flash", "glm-5.3",
                      "kimi-k2", "MiMo-7B", "ling-flash"):
            self.assertTrue(should_echo_reasoning(model, "https://api.example.com/v1",
                                                   "auto"), model)

    def test_auto_detects_echo_families_by_base_url(self) -> None:
        self.assertTrue(should_echo_reasoning(
            "custom-model", "https://api.moonshot.cn/v1", "auto"))
        self.assertTrue(should_echo_reasoning(
            "custom-model", "https://open.bigmodel.cn/api/paas/v4", "auto"))

    def test_auto_strips_for_everyone_else(self) -> None:
        self.assertFalse(should_echo_reasoning("gpt-4o",
                                                "https://api.openai.com/v1", "auto"))
        self.assertFalse(should_echo_reasoning("mistral-large", None, "auto"))

    def test_reads_mode_from_env_when_not_pinned(self) -> None:
        with patch.dict(os.environ, {"AGENT_RUNTIME_REASONING_ECHO": "never"}):
            self.assertFalse(should_echo_reasoning("deepseek-v4", None))
        with patch.dict(os.environ, {"AGENT_RUNTIME_REASONING_ECHO": "always"}):
            self.assertTrue(should_echo_reasoning("gpt-4o", None))

    def test_a_gateway_never_gets_the_reasoning_replayed(self) -> None:
        # OpenRouter serves "deepseek/..." models but drops the assistant
        # turn's reasoning before it reaches the upstream model, so the
        # model-name heuristic must not win here.
        self.assertFalse(should_echo_reasoning(
            "deepseek/deepseek-v4.1-flash", "https://openrouter.ai/api/v1", "auto"))

    def test_a_gateway_stays_stripped_even_for_a_echo_family_model(self) -> None:
        for model in ("glm-5.3", "kimi-k2", "deepseek-chat"):
            self.assertFalse(should_echo_reasoning(
                model, "https://openrouter.ai/api/v1", "auto"), model)

    def test_the_pinned_modes_still_win_over_the_gateway_rule(self) -> None:
        self.assertTrue(should_echo_reasoning(
            "deepseek/deepseek-v4.1-flash", "https://openrouter.ai/api/v1", "always"))
        self.assertFalse(should_echo_reasoning(
            "deepseek-chat", "https://api.deepseek.com/v1", "never"))


class ReasoningTextTests(unittest.TestCase):
    class _Payload:
        def __init__(self, **fields: Any) -> None:
            for name, value in fields.items():
                setattr(self, name, value)

    def test_reads_the_deepseek_field(self) -> None:
        payload = self._Payload(reasoning_content="inner monologue")

        self.assertEqual(_reasoning_text(payload), "inner monologue")

    def test_reads_the_openrouter_field(self) -> None:
        payload = self._Payload(reasoning="thinking out loud")

        self.assertEqual(_reasoning_text(payload), "thinking out loud")

    def test_reads_the_structured_details_array(self) -> None:
        payload = self._Payload(reasoning_details=[
            {"type": "reasoning.text", "text": "part one "},
            {"type": "reasoning.text", "text": "part two"},
        ])

        self.assertEqual(_reasoning_text(payload), "part one part two")

    def test_the_native_field_wins_over_the_gateway_one(self) -> None:
        payload = self._Payload(reasoning_content="native", reasoning="gateway")

        self.assertEqual(_reasoning_text(payload), "native")

    def test_no_reasoning_reads_as_empty(self) -> None:
        self.assertEqual(_reasoning_text(self._Payload()), "")
        self.assertEqual(_reasoning_text(self._Payload(reasoning="")), "")
        self.assertEqual(_reasoning_text(self._Payload(reasoning_details=[])), "")


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

    def test_runtime_origin_metadata_never_reaches_the_wire(self) -> None:
        message = runtime_user_message("retry the tool call",
                                       kind="tool_rejection")

        projected = _project_messages_for_wire([message], echo=False)

        self.assertEqual(projected[0]["role"], "user")
        self.assertEqual(projected[0]["content"], "retry the tool call")
        self.assertNotIn("metadata", projected[0])

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

    def test_dangling_tool_call_is_dropped_answered_call_kept(self) -> None:
        history: list[dict[str, Any]] = [
            {"role": "assistant", "content": "finishing",
             "tool_calls": [
                 {"id": "work", "type": "function",
                  "function": {"name": "run_command", "arguments": "{}"}},
                 {"id": "stray", "type": "function",
                  "function": {"name": "read_file", "arguments": "{}"}},
             ]},
            {"role": "tool", "tool_call_id": "work", "content": "out"},
        ]

        projected = _project_messages_for_wire(history, echo=True)

        self.assertEqual(
            [call["id"] for call in projected[0]["tool_calls"]], ["work"])
        # The stored history is untouched; only the wire copy is repaired.
        self.assertEqual(len(history[0]["tool_calls"]), 2)

    def test_fully_dangling_call_leaves_content_without_tool_calls(self) -> None:
        history: list[dict[str, Any]] = [
            {"role": "assistant", "content": "done",
             "tool_calls": [{"id": "stray", "type": "function",
                             "function": {"name": "read_file",
                                          "arguments": "{}"}}]},
            {"role": "user", "content": "runtime nudge"},
        ]

        projected = _project_messages_for_wire(history, echo=True)

        self.assertEqual(projected[0]["content"], "done")
        self.assertNotIn("tool_calls", projected[0])

    def test_empty_assistant_with_only_dangling_calls_gets_placeholder(self) -> None:
        history: list[dict[str, Any]] = [
            {"role": "assistant", "content": "",
             "tool_calls": [{"id": "stray", "type": "function",
                             "function": {"name": "read_file",
                                          "arguments": "{}"}}]},
        ]

        projected = _project_messages_for_wire(history, echo=False)

        self.assertNotIn("tool_calls", projected[0])
        self.assertEqual(projected[0]["content"], "(reasoning omitted)")

    def test_paired_history_passes_through_unchanged(self) -> None:
        history = self._history()

        projected = _project_messages_for_wire(history, echo=True)

        self.assertEqual(
            [call["id"] for call in projected[2]["tool_calls"]], ["c1"])
        self.assertEqual(projected[3]["tool_call_id"], "c1")

    def test_adjacent_out_of_order_replies_stay_paired(self) -> None:
        # A valid block may list its tool replies in any order directly
        # after the assistant.  Repair must keep every reply and keep the
        # block contiguous, without inventing a pairing violation.
        history: list[dict[str, Any]] = [
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "a", "type": "function",
                 "function": {"name": "run_command", "arguments": "{}"}},
                {"id": "b", "type": "function",
                 "function": {"name": "run_command", "arguments": "{}"}},
            ]},
            {"role": "tool", "tool_call_id": "b", "content": "B"},
            {"role": "tool", "tool_call_id": "a", "content": "A"},
        ]

        projected = _project_messages_for_wire(history, echo=True)

        self.assertEqual(
            [message["role"] for message in projected],
            ["assistant", "tool", "tool"],
        )
        self.assertEqual(
            {message["tool_call_id"] for message in projected[1:]},
            {"a", "b"},
        )
        self.assertEqual(
            [call["id"] for call in projected[0]["tool_calls"]], ["a", "b"])
        self.assertEqual(history[1]["tool_call_id"], "b")

    def test_split_tool_pair_is_moved_after_assistant(self) -> None:
        # tb-v9 regression: the response exists, but a runtime user message
        # separates it from the assistant call.  The wire copy must put the
        # tool reply first and keep the stored history untouched.
        history: list[dict[str, Any]] = [
            {"role": "user", "content": "task"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "c1", "type": "function",
                 "function": {"name": "run_command", "arguments": "{}"}}]},
            runtime_user_message(
                "Tool call to bash was rejected and not executed. "
                "Please retry.",
                kind="tool_rejection",
            ),
            {"role": "tool", "tool_call_id": "c1", "content": "out"},
        ]

        projected = _project_messages_for_wire(history, echo=True)

        self.assertEqual(
            [message["role"] for message in projected],
            ["user", "assistant", "tool", "user"],
        )
        self.assertEqual(projected[2]["tool_call_id"], "c1")
        self.assertEqual(projected[3]["content"],
                         "Tool call to bash was rejected and not executed. "
                         "Please retry.")
        self.assertNotIn("metadata", projected[3])
        # Stored history keeps its original order.
        self.assertEqual(history[2]["role"], "user")

    def test_orphan_tool_reply_is_dropped(self) -> None:
        history: list[dict[str, Any]] = [
            {"role": "user", "content": "task"},
            {"role": "assistant", "content": "done"},
            {"role": "tool", "tool_call_id": "ghost", "content": "out"},
        ]

        projected = _project_messages_for_wire(history, echo=True)

        self.assertEqual([message["role"] for message in projected],
                         ["user", "assistant"])
        self.assertEqual(len(history), 3)

    def test_batch_reorders_replies_and_drops_unanswered_calls(self) -> None:
        history: list[dict[str, Any]] = [
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "a", "type": "function",
                 "function": {"name": "run_command", "arguments": "{}"}},
                {"id": "stray", "type": "function",
                 "function": {"name": "read_file", "arguments": "{}"}},
                {"id": "b", "type": "function",
                 "function": {"name": "run_command", "arguments": "{}"}},
            ]},
            runtime_user_message("rejected", kind="tool_rejection"),
            {"role": "tool", "tool_call_id": "b", "content": "B"},
            {"role": "tool", "tool_call_id": "a", "content": "A"},
        ]

        projected = _project_messages_for_wire(history, echo=True)

        self.assertEqual(
            [message["role"] for message in projected],
            ["assistant", "tool", "tool", "user"],
        )
        self.assertEqual(
            [call["id"] for call in projected[0]["tool_calls"]], ["a", "b"])
        self.assertEqual(
            [message["tool_call_id"] for message in projected[1:3]],
            ["a", "b"],
        )
        self.assertEqual(len(history[0]["tool_calls"]), 3)


class _FakeMessage:
    def __init__(self, reasoning: str = "") -> None:
        self.content = "ok"
        self.tool_calls: list[Any] = []
        self.reasoning_content = None
        self.reasoning = reasoning
        self.reasoning_details: list[Any] = []


class _FakeCompletions:
    def __init__(self, reasoning: str = "",
                 finish_reason: str | None = None) -> None:
        self.calls: list[dict[str, Any]] = []
        self.reasoning = reasoning
        self.finish_reason = finish_reason

    async def create(self, **kwargs: Any) -> Any:
        self.calls.append(kwargs)
        if kwargs.get("stream"):
            async def _chunks() -> Any:
                yield _Chunk("ok", "stop", reasoning=self.reasoning)
            return _chunks()
        response = unittest.mock.MagicMock()
        choice = unittest.mock.MagicMock(
            message=_FakeMessage(reasoning=self.reasoning))
        choice.finish_reason = self.finish_reason
        response.choices = [choice]
        return response


class _Chunk:
    def __init__(self, content: str, finish_reason: str | None = None,
                 reasoning: str = "") -> None:
        delta = unittest.mock.MagicMock()
        delta.content = content
        delta.reasoning_content = ""
        delta.reasoning = reasoning
        delta.reasoning_details = []
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

    async def test_chat_exposes_finish_reason(self) -> None:
        self.completions.finish_reason = "length"

        result = await self.llm.chat([])

        self.assertEqual(result["finish_reason"], "length")

    async def test_chat_omits_finish_reason_when_unknown(self) -> None:
        result = await self.llm.chat([])

        self.assertNotIn("finish_reason", result)

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


class ProviderReasoningTests(unittest.IsolatedAsyncioTestCase):
    """A gateway that answers with ``reasoning`` instead of
    ``reasoning_content`` must still reach the runtime as reasoning."""

    def _llm(self, reasoning: str) -> OpenAICompatibleLLM:
        completions = _FakeCompletions(reasoning=reasoning)
        llm = OpenAICompatibleLLM(api_key="test-key",
                                  base_url="https://openrouter.ai/api/v1",
                                  model="deepseek/deepseek-v4.1-flash")
        llm.client = unittest.mock.MagicMock()
        llm.client.chat.completions = completions
        return llm

    async def test_stream_reads_the_gateway_reasoning_field(self) -> None:
        llm = self._llm("thinking out loud")

        chunks = [chunk async for chunk in llm.stream([])]

        self.assertEqual(chunks[0]["reasoning_content"], "thinking out loud")

    async def test_a_reasoning_only_chunk_is_not_dropped(self) -> None:
        completions = _FakeCompletions(reasoning="thinking out loud")
        llm = OpenAICompatibleLLM(api_key="test-key",
                                  base_url="https://openrouter.ai/api/v1",
                                  model="deepseek/deepseek-v4.1-flash")
        llm.client = unittest.mock.MagicMock()
        llm.client.chat.completions = completions

        async def _chunks() -> Any:
            yield _Chunk("", None, reasoning="thinking out loud")
        completions.create = unittest.mock.AsyncMock(return_value=_chunks())

        chunks = [chunk async for chunk in llm.stream([])]

        self.assertEqual(chunks[0]["reasoning_content"], "thinking out loud")

    async def test_chat_reads_the_gateway_reasoning_field(self) -> None:
        llm = self._llm("thinking out loud")

        result = await llm.chat([])

        self.assertEqual(result["reasoning_content"], "thinking out loud")


class _ToolCallChunk:
    """A stream chunk carrying one provider delta tool call."""

    def __init__(self, tool_call: Any,
                 finish_reason: str | None = None) -> None:
        delta = unittest.mock.MagicMock()
        delta.content = ""
        delta.reasoning_content = ""
        delta.reasoning = ""
        delta.reasoning_details = []
        delta.tool_calls = [tool_call]
        self.choices = [unittest.mock.MagicMock(delta=delta,
                                                finish_reason=finish_reason)]


def _delta_tool_call(call_id: str, arguments: str,
                     signature: dict | None = None) -> Any:
    """Build a loosely-parsed delta call, as the SDK does for SSE data."""
    extra = {"extra_content": signature} if signature else {}
    return ChoiceDeltaToolCall.model_construct(
        index=0, id=call_id,
        function=ChoiceDeltaToolCallFunction.model_construct(
            name="read_file", arguments=arguments),
        **extra)


class ProviderToolCallExtrasTests(unittest.IsolatedAsyncioTestCase):
    """Per-call provider extras (Gemini thought_signature) must survive."""

    def _llm(self, completions: Any) -> OpenAICompatibleLLM:
        llm = OpenAICompatibleLLM(api_key="test-key",
                                  base_url="https://api.example.com/v1",
                                  model="gemini-4-neon")
        llm.client = unittest.mock.MagicMock()
        llm.client.chat.completions = completions
        return llm

    async def test_stream_yields_the_whole_provider_delta(self) -> None:
        signature = {"google": {"thought_signature": "SIG"}}

        async def _chunks() -> Any:
            yield _ToolCallChunk(_delta_tool_call("call_1", "{}", signature))
            yield _Chunk("", "tool_calls")

        completions = unittest.mock.MagicMock()
        completions.create = unittest.mock.AsyncMock(return_value=_chunks())

        chunks = [chunk async for chunk in self._llm(completions).stream([])]

        delta = chunks[0]["tool_calls"][0]
        self.assertEqual(delta["extra_content"], signature)
        self.assertEqual(delta["id"], "call_1")
        self.assertEqual(delta["function"]["name"], "read_file")
        self.assertEqual(delta["index"], 0)

    async def test_chat_keeps_the_provider_extras(self) -> None:
        signature = {"google": {"thought_signature": "SIG"}}
        tool_call = ChatCompletionMessageToolCall.model_construct(
            id="call_1", type="function",
            function=Function.model_construct(name="read_file",
                                              arguments="{}"),
            extra_content=signature)
        message = _FakeMessage()
        message.tool_calls = [tool_call]
        response = unittest.mock.MagicMock(usage=None)
        response.choices = [unittest.mock.MagicMock(message=message,
                                                    finish_reason="tool_calls")]
        completions = unittest.mock.MagicMock()
        completions.create = unittest.mock.AsyncMock(return_value=response)

        result = await self._llm(completions).chat([])

        self.assertEqual(result["tool_calls"][0]["extra_content"], signature)

    async def test_history_extras_reach_the_request_body(self) -> None:
        """The SDK must not filter unknown keys off a tool call."""
        signature = {"google": {"thought_signature": "SIG"}}
        sent: dict[str, Any] = {}

        def handler(request: Any) -> Any:
            sent.update(json.loads(request.content))
            return httpx.Response(200, json={
                "id": "x", "object": "chat.completion", "created": 0,
                "model": "gemini-4-neon",
                "choices": [{"index": 0, "finish_reason": "stop",
                              "message": {"role": "assistant",
                                          "content": "ok"}}],
            })

        http_client = httpx.AsyncClient(
            transport=httpx.MockTransport(handler))
        self.addAsyncCleanup(http_client.aclose)
        llm = OpenAICompatibleLLM(api_key="test-key",
                                  base_url="https://api.example.com/v1",
                                  model="gemini-4-neon",
                                  http_client=http_client)
        history = [
            {"role": "user", "content": "go"},
            {"role": "assistant", "content": "", "tool_calls": [
                {"id": "call_1", "type": "function",
                 "function": {"name": "read_file", "arguments": "{}"},
                 "extra_content": signature}]},
            {"role": "tool", "tool_call_id": "call_1", "content": "{}"},
        ]

        await llm.chat(history)

        echoed = sent["messages"][1]["tool_calls"][0]
        self.assertEqual(echoed["extra_content"], signature)


if __name__ == "__main__":
    import sys as _sys
    from pathlib import Path as _Path

    # A direct run skips ``conftest.py``: isolate the runtime home here too.
    _sys.path.insert(0, str(next(parent for parent in
                                 _Path(__file__).resolve().parents
                                 if (parent / "conftest.py").is_file())))
    from _direct_run import isolate_runtime_home

    isolate_runtime_home()
    unittest.main()
