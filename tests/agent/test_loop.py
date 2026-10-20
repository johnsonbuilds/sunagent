import sys
import json
import tempfile
import unittest
from os import environ
from pathlib import Path
from typing import Any
from unittest import mock


sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

import agent_runtime.agent.chat as chat_module
import agent_runtime.agent.loop as loop_module
from agent_runtime.agent.loop import AgentTurn, Conversation, run_turn, use_streaming
from agent_runtime.harness import (
    ControlGenome,
    HarnessSpec,
    LLM_RETRY_CATEGORIES,
    LLMRetryPolicy,
    PromptGenome,
    RecoveryGenome,
    default_harness,
)
from agent_runtime.trace import RunTrace
from agent_runtime.tools.tools import ToolRegistry, ToolSpec


# The runtime-mechanics tests pin the neutral built-in genome: they
# exercise the loop, not the default manifest, so they must not drift
# when harnesses/common-v0.yaml changes.
NEUTRAL_HARNESS = HarnessSpec()


WEATHER_SCHEMA = {
    "type": "object",
    "properties": {"location": {"type": "string"}},
    "required": ["location"],
}


class FakeLLM:
    def __init__(self, responses: list[dict[str, Any]] | None = None,
                 error: Exception | None = None) -> None:
        self.responses = responses or []
        self.error = error
        self.messages: list[list[dict[str, Any]]] = []

    async def chat(self, messages: list[dict[str, Any]],
                   tools: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        self.messages.append(messages.copy())
        if self.error:
            raise self.error
        return self.responses.pop(0)


class StreamingLLM:
    def __init__(self, responses: list[list[dict[str, Any]]]) -> None:
        self.responses = responses
        self.messages: list[list[dict[str, Any]]] = []

    async def chat(self, messages: list[dict[str, Any]],
                   tools: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        raise AssertionError("the runtime should use stream()")

    async def stream(self, messages: list[dict[str, Any]],
                     tools: list[dict[str, Any]] | None = None):
        self.messages.append(messages.copy())
        for chunk in self.responses.pop(0):
            yield chunk


async def default_handler(location: str) -> str:
    return location


def make_registry(handler: Any = default_handler) -> ToolRegistry:
    return ToolRegistry([ToolSpec("weather", "", WEATHER_SCHEMA, handler)])


class AgentLoopErrorHandlingTests(unittest.IsolatedAsyncioTestCase):
    async def test_streaming_response_preserves_tool_loop_semantics(self) -> None:
        llm = StreamingLLM([
            [
                {"content": "", "reasoning_content": "thinking about weather",
                 "tool_calls": []},
                {"content": "Checking ", "tool_calls": []},
                {"content": "weather", "tool_calls": []},
                {"tool_calls": [{"index": 0, "id": "1",
                                  "function": {"name": "weather", "arguments": "{"}}]},
                {"tool_calls": [{"index": 0,
                                  "function": {"arguments": "\"location\":\"Singapore\"}"}}]},
                {"finish_reason": "tool_calls"},
            ],
            [{"content": "It is sunny", "tool_calls": []},
             {"finish_reason": "stop"}],
        ])
        trace = RunTrace(run_id="streaming")

        self.assertEqual(await run_turn("weather", llm, make_registry(), stream=True,
                                        trace=trace, harness=NEUTRAL_HARNESS),
                         "It is sunny")
        self.assertEqual(
            [event.event_type for event in trace.events],
            ["agent.start", "llm.start", "llm.chunk", "llm.chunk", "llm.chunk",
             "llm.chunk", "llm.chunk", "llm.stream.finish", "llm.end",
             "tool.start", "tool.end",
             "llm.start", "llm.chunk", "llm.stream.finish", "llm.end",
             "agent.end"],
        )
        self.assertEqual(llm.messages[1][-1]["content"], "Singapore")
        self.assertEqual(llm.messages[1][1]["tool_calls"][0]["type"], "function")
        self.assertEqual(llm.messages[1][1]["reasoning_content"],
                         "thinking about weather")
        reasoning_chunk = trace.events[2]
        self.assertEqual(reasoning_chunk.data["reasoning_chars"],
                         len("thinking about weather"))
        first_llm_end = next(event for event in trace.events
                             if event.event_type == "llm.end")
        self.assertEqual(first_llm_end.data["tool_count"], 1)

    async def test_run_trace_records_agent_loop_and_tool_events(self) -> None:
        llm = FakeLLM([
            {"content": "", "tool_calls": [{"id": "1", "function": {
                "name": "weather", "arguments": '{"location":"Singapore"}'}}]},
            {"content": "It is sunny", "tool_calls": []},
        ])
        trace = RunTrace(run_id="r1")

        self.assertEqual(await run_turn("weather", llm, make_registry(), trace=trace),
                         "It is sunny")
        self.assertEqual(
            [event.event_type for event in trace.events],
            ["agent.start", "llm.start", "llm.end", "tool.start", "tool.end",
             "llm.start", "llm.end", "agent.end"],
        )
        self.assertEqual({event.run_id for event in trace.events}, {"r1"})
        self.assertEqual(len({event.event_id for event in trace.events}), len(trace.events))
        llm_end = next(event for event in trace.events
                       if event.event_type == "llm.end")
        self.assertEqual(llm_end.data["tool_count"], 1)

    async def test_truncated_stream_without_finish_reason_fails_the_turn(self) -> None:
        llm = StreamingLLM([
            [{"content": "", "reasoning_content": "thinking very long",
              "tool_calls": []}],
        ])
        answer = await run_turn("weather", llm, make_registry(), stream=True,
                                harness=NEUTRAL_HARNESS)
        self.assertIn("finish_reason", answer)
        self.assertIn("LLM error", answer)

    async def test_stream_with_no_payload_at_all_fails_the_turn(self) -> None:
        llm = StreamingLLM([
            [{"finish_reason": "stop"}],
        ])
        answer = await run_turn("weather", llm, make_registry(), stream=True,
                                harness=NEUTRAL_HARNESS)
        self.assertIn("without any content, reasoning, or tool calls", answer)
        self.assertIn("LLM error", answer)

    async def test_reasoning_budget_cut_appends_nudge_and_retries(self) -> None:
        environ["AGENT_RUNTIME_MAX_REASONING_CHARS"] = "500"
        try:
            llm = StreamingLLM([
                # Turn 1: thinking-only spiral past the budget.
                [{"content": "", "reasoning_content": "x" * 600,
                  "tool_calls": []}],
                # Turn 2: after the nudge the model acts.
                [{"content": "", "tool_calls": [
                    {"index": 0, "id": "1", "function": {
                        "name": "weather",
                        "arguments": '{"location":"Singapore"}'}}]},
                 {"finish_reason": "tool_calls"}],
                # Turn 3: final answer.
                [{"content": "It is sunny", "tool_calls": []},
                 {"finish_reason": "stop"}],
            ])
            trace = RunTrace(run_id="budget")

            answer = await run_turn("weather", llm, make_registry(),
                                    stream=True, trace=trace)

            self.assertEqual(answer, "It is sunny")
            # The nudge landed in the conversation before the retry.
            nudges = [m for m in llm.messages[1]
                      if m["role"] == "user" and "Stop reasoning and act" in m["content"]]
            self.assertEqual(len(nudges), 1)
            budget_events = [e for e in trace.events
                             if e.event_type == "llm.reasoning_budget"]
            self.assertEqual(len(budget_events), 1)
            self.assertEqual(budget_events[0].data["reasoning_chars"], 600)
        finally:
            environ.pop("AGENT_RUNTIME_MAX_REASONING_CHARS", None)

    async def test_reasoning_budget_ignores_turns_that_act(self) -> None:
        environ["AGENT_RUNTIME_MAX_REASONING_CHARS"] = "500"
        try:
            llm = StreamingLLM([
                # Long reasoning, but the turn ends with a tool call:
                # the budget must not cut it.
                [{"content": "", "reasoning_content": "x" * 600,
                  "tool_calls": [
                      {"index": 0, "id": "1", "function": {
                          "name": "weather",
                          "arguments": '{"location":"Singapore"}'}}]},
                 {"finish_reason": "tool_calls"}],
                [{"content": "It is sunny", "tool_calls": []},
                 {"finish_reason": "stop"}],
            ])

            answer = await run_turn("weather", llm, make_registry(), stream=True)

            self.assertEqual(answer, "It is sunny")
            nudges = [m for m in llm.messages[1]
                      if m["role"] == "user" and "Stop reasoning and act" in m["content"]]
            self.assertEqual(len(nudges), 0)
        finally:
            environ.pop("AGENT_RUNTIME_MAX_REASONING_CHARS", None)

    async def test_reasoning_budget_zero_disables_guard(self) -> None:
        environ["AGENT_RUNTIME_MAX_REASONING_CHARS"] = "0"
        try:
            llm = StreamingLLM([
                [{"content": "", "reasoning_content": "x" * 600,
                  "tool_calls": []},
                 {"finish_reason": "stop"}],
            ])

            answer = await run_turn("weather", llm, make_registry(), stream=True)

            # No nudge: the guard was off, and the (empty) turn completed
            # as the final answer after a single request.
            self.assertEqual(len(llm.messages), 1)
            self.assertEqual(answer, "")
        finally:
            environ.pop("AGENT_RUNTIME_MAX_REASONING_CHARS", None)

    def test_trace_span_records_lifecycle(self) -> None:
        trace = RunTrace(run_id="r1")
        with trace.span("operation", iteration=2, request_id="req-1") as meta:
            meta["result_count"] = 3

        with self.assertRaisesRegex(RuntimeError, "failed"):
            with trace.span("failing_operation"):
                raise RuntimeError("failed")

        self.assertEqual(
            [event.event_type for event in trace.events],
            ["operation.start", "operation.end", "failing_operation.start",
             "failing_operation.error", "failing_operation.end"],
        )
        self.assertEqual(trace.events[1].data["result_count"], 3)
        self.assertIn("duration_ms", trace.events[1].data)
        self.assertEqual(trace.events[3].data["error"], "failed")

    async def test_run_trace_writes_jsonl(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "runs" / "run-001.jsonl"
            trace = RunTrace(run_id="r1", output_path=path)
            trace.emit("agent.start")
            await trace.flush()

            records = [json.loads(line) for line in path.read_text().splitlines()]
            self.assertEqual(records[0]["event_type"], "agent.start")
            self.assertEqual(records[0]["run_id"], "r1")

    async def test_malformed_json_becomes_observation_and_loop_continues(self) -> None:
        llm = FakeLLM([
            {"content": "", "tool_calls": [{"id": "1", "function": {
                "name": "weather", "arguments": "{"}}]},
            {"content": "recovered", "tool_calls": []},
        ])

        self.assertEqual(await run_turn("weather", llm, make_registry()), "recovered")
        self.assertIn("malformed JSON", llm.messages[1][-1]["content"])

    async def test_invalid_tool_call_never_enters_conversation_history(self) -> None:
        llm = FakeLLM([
            {"content": "I'll check the weather.", "tool_calls": [{"id": "1", "function": {
                "name": "weather", "arguments": "{}"}}]},
            {"content": "recovered", "tool_calls": []},
        ])

        self.assertEqual(await run_turn("weather", llm, make_registry()), "recovered")
        second_request = llm.messages[1]
        assistant_messages = [message for message in second_request
                              if message["role"] == "assistant"]
        self.assertEqual(len(assistant_messages), 1)
        self.assertNotIn("tool_calls", assistant_messages[0])
        self.assertEqual(assistant_messages[0]["content"], "I'll check the weather.")
        self.assertEqual(second_request[-1]["role"], "user")
        self.assertIn("missing required argument: location",
                      second_request[-1]["content"])
        self.assertIn("weather", second_request[-1]["content"])
        self.assertIn("retry this tool call", second_request[-1]["content"])

    async def test_replayed_tool_calls_are_always_valid_json(self) -> None:
        llm = FakeLLM([
            {"content": "", "tool_calls": [
                {"id": "1", "function": {"name": "weather", "arguments": "{"}},
                {"id": "2", "function": {"name": "weather", "arguments": "{}"}},
                {"function": {"name": "weather", "arguments": '{"location":"Paris"}'}},
            ]},
            {"content": "recovered", "tool_calls": []},
        ])

        self.assertEqual(await run_turn("weather", llm, make_registry()), "recovered")
        for request in llm.messages:
            for message in request:
                for tool_call in message.get("tool_calls") or []:
                    parsed = json.loads(tool_call["function"]["arguments"])
                    self.assertIsInstance(parsed, dict)

    async def test_mixed_response_keeps_valid_call_and_drops_invalid(self) -> None:
        llm = FakeLLM([
            {"content": "Inspecting.", "tool_calls": [
                {"id": "good", "function": {
                    "name": "weather", "arguments": '{"location":"Singapore"}'}},
                {"id": "bad", "function": {
                    "name": "weather", "arguments": "{"}},
            ]},
            {"content": "done", "tool_calls": []},
        ])

        self.assertEqual(await run_turn("weather", llm, make_registry()), "done")
        second_request = llm.messages[1]
        assistant = next(message for message in second_request
                         if message["role"] == "assistant")
        self.assertEqual(assistant["content"], "Inspecting.")
        self.assertEqual(len(assistant["tool_calls"]), 1)
        self.assertEqual(assistant["tool_calls"][0]["id"], "good")
        self.assertEqual(assistant["tool_calls"][0]["type"], "function")
        self.assertEqual(json.loads(assistant["tool_calls"][0]["function"]["arguments"]),
                         {"location": "Singapore"})
        tool_messages = [message for message in second_request
                         if message["role"] == "tool"]
        self.assertEqual([message["tool_call_id"] for message in tool_messages],
                         ["good"])
        self.assertEqual(tool_messages[0]["content"], "Singapore")
        rejected = [message for message in second_request
                    if message["role"] == "user" and "malformed JSON" in message["content"]]
        self.assertEqual(len(rejected), 1)

    async def test_rejected_tool_call_records_trace_events(self) -> None:
        llm = FakeLLM([
            {"content": "", "tool_calls": [{"id": "1", "function": {
                "name": "weather", "arguments": "{}"}}]},
            {"content": "recovered", "tool_calls": []},
        ])
        trace = RunTrace(run_id="r1")

        self.assertEqual(await run_turn("weather", llm, make_registry(), trace=trace),
                         "recovered")
        self.assertEqual(
            [event.event_type for event in trace.events],
            ["agent.start", "llm.start", "llm.end", "tool.start",
             "tool.error", "tool.end", "llm.start", "llm.end", "agent.end"],
        )
        tool_error = next(event for event in trace.events
                          if event.event_type == "tool.error")
        self.assertEqual(tool_error.data["error"], "missing required argument: location")
        tool_end = next(event for event in trace.events
                        if event.event_type == "tool.end")
        self.assertEqual(tool_end.data["status"], "error")

    async def test_unknown_tool_and_invalid_arguments_are_observations(self) -> None:
        llm = FakeLLM([
            {"content": "", "tool_calls": [
                {"id": "unknown", "function": {"name": "missing", "arguments": "{}"}},
                {"id": "bad", "function": {"name": "weather", "arguments": "{\"location\": 1}"}},
            ]},
            {"content": "recovered", "tool_calls": []},
        ])

        self.assertEqual(await run_turn("weather", llm, make_registry()), "recovered")
        observations = [message["content"] for message in llm.messages[1][-2:]]
        self.assertIn("unknown tool", observations[0])
        self.assertIn("must be string", observations[1])

    async def test_tool_exception_becomes_observation(self) -> None:
        async def failing_tool(location: str) -> str:
            raise RuntimeError("service unavailable")

        llm = FakeLLM([
            {"content": "", "tool_calls": [{"id": "1", "function": {
                "name": "weather", "arguments": '{"location":"Paris"}'}}]},
            {"content": "fallback", "tool_calls": []},
        ])

        self.assertEqual(await run_turn("weather", llm, make_registry(failing_tool)), "fallback")
        self.assertIn("service unavailable", llm.messages[1][-1]["content"])

    async def test_tool_trace_records_error_and_loop_continues(self) -> None:
        async def failing_tool(location: str) -> str:
            raise RuntimeError("service unavailable")

        llm = FakeLLM([
            {"content": "", "tool_calls": [{"id": "1", "function": {
                "name": "weather", "arguments": '{"location":"Paris"}'}}]},
            {"content": "fallback", "tool_calls": []},
        ])
        trace = RunTrace(run_id="r1")

        self.assertEqual(await run_turn("weather", llm, make_registry(failing_tool),
                                        trace=trace), "fallback")
        self.assertEqual(
            [event.event_type for event in trace.events],
            ["agent.start", "llm.start", "llm.end", "tool.start",
             "tool.error", "tool.end", "llm.start", "llm.end", "agent.end"],
        )
        tool_error = next(event for event in trace.events
                          if event.event_type == "tool.error")
        self.assertEqual(tool_error.data["error"], "service unavailable")
        self.assertEqual(tool_error.data["tool"], "weather")
        self.assertEqual(len(llm.messages), 2)
        self.assertIn("service unavailable", llm.messages[1][-1]["content"])

    async def test_missing_tool_call_id_does_not_crash(self) -> None:
        llm = FakeLLM([
            {"content": "", "tool_calls": [{"function": {
                "name": "weather", "arguments": '{"location":"Paris"}'}}]},
            {"content": "recovered", "tool_calls": []},
        ])

        self.assertEqual(await run_turn("weather", llm, make_registry()), "recovered")
        self.assertEqual(llm.messages[1][-1]["role"], "user")
        self.assertIn("tool_call_id is required", llm.messages[1][-1]["content"])
        self.assertIn("retry this tool call with a valid tool_call_id", llm.messages[1][-1]["content"])

    async def test_llm_exception_is_returned(self) -> None:
        trace = RunTrace(run_id="r1")
        self.assertEqual(
            await run_turn("hello", FakeLLM(error=RuntimeError("network down")),
                           make_registry(), trace=trace,
                           harness=NEUTRAL_HARNESS),
            "LLM error: network down",
        )
        self.assertEqual(
            [event.event_type for event in trace.events],
            ["agent.start", "llm.start", "llm.error", "llm.end",
             "agent.error", "agent.end"],
        )
        llm_error = next(event for event in trace.events
                         if event.event_type == "llm.error")
        self.assertEqual(llm_error.data["error"], "network down")
        agent_error = next(event for event in trace.events
                           if event.event_type == "agent.error")
        self.assertEqual(agent_error.data["error"], "LLM error: network down")
        self.assertEqual(agent_error.data["stage"], "llm")

    async def test_conversation_preserves_context_between_turns(self) -> None:
        llm = FakeLLM([
            {"content": "first answer", "tool_calls": []},
            {"content": "second answer", "tool_calls": []},
        ])
        conversation = Conversation()

        self.assertEqual(await run_turn("user input 1", llm, make_registry(),
                                        conversation=conversation),
                         "first answer")
        self.assertEqual(await run_turn("user input 2", llm, make_registry(),
                                        conversation=conversation),
                         "second answer")
        self.assertEqual([message["content"] for message in conversation.messages
                          if message["role"] == "user"], ["user input 1", "user input 2"])
        self.assertEqual([message["content"] for message in llm.messages[1]
                          if message["role"] == "user"], ["user input 1", "user input 2"])
        self.assertNotIn("tool_calls", conversation.messages[1])


class HarnessIntegrationTests(unittest.IsolatedAsyncioTestCase):
    async def test_default_harness_sends_its_system_prompt(self) -> None:
        llm = FakeLLM([{"content": "hi", "tool_calls": []}])

        await run_turn("hello", llm, make_registry())

        self.assertEqual(llm.messages[0][0]["role"], "system")
        self.assertEqual(llm.messages[0][0]["content"],
                         default_harness().prompt.system)

    async def test_system_prompt_gene_is_injected_before_user_message(self) -> None:
        llm = FakeLLM([{"content": "hi", "tool_calls": []},
                       {"content": "again", "tool_calls": []}])
        harness = HarnessSpec(id="with-system",
                              prompt=PromptGenome(system="You are a shell agent."))
        conversation = Conversation()

        await run_turn("hello", llm, make_registry(), harness=harness,
                       conversation=conversation)
        await run_turn("again", llm, make_registry(), harness=harness,
                       conversation=conversation)

        self.assertEqual(llm.messages[0][0],
                         {"role": "system", "content": "You are a shell agent."})
        self.assertEqual(llm.messages[0][1]["role"], "user")
        self.assertEqual([m["role"] for m in conversation.messages].count("system"), 1)

    async def test_iteration_limit_notice_comes_from_harness(self) -> None:
        notice = "CUSTOM STOP NOTICE."
        harness = HarnessSpec(prompt=PromptGenome(iteration_limit_notice=notice))
        llm = FakeLLM([
            {"content": "", "tool_calls": [{"id": "1", "function": {
                "name": "weather", "arguments": '{"location":"Oslo"}'}}]},
            {"content": "", "tool_calls": [{"id": "2", "function": {
                "name": "weather", "arguments": '{"location":"Rome"}'}}]},
            {"content": "summarized", "tool_calls": []},
        ])

        answer = await run_turn("weather", llm, make_registry(), harness=harness,
                                max_iterations=2)

        self.assertEqual(answer, "summarized")
        self.assertEqual(llm.messages[2][-1],
                         {"role": "user", "content": notice})

    async def test_max_iterations_defaults_to_harness_control_gene(self) -> None:
        harness = HarnessSpec(control=ControlGenome(max_iterations=1))
        llm = FakeLLM([
            {"content": "", "tool_calls": [{"id": "1", "function": {
                "name": "weather", "arguments": '{"location":"Oslo"}'}}]},
            {"content": "summarized", "tool_calls": []},
        ])

        answer = await run_turn("weather", llm, make_registry(), harness=harness)

        self.assertEqual(answer, "summarized")
        self.assertEqual(len(llm.messages), 2)

    async def test_recovery_gene_formats_tool_error_observation(self) -> None:
        async def failing_tool(location: str) -> str:
            raise RuntimeError("service unavailable")

        llm = FakeLLM([
            {"content": "", "tool_calls": [{"id": "1", "function": {
                "name": "weather", "arguments": '{"location":"Paris"}'}}]},
            {"content": "fallback", "tool_calls": []},
        ])

        await run_turn("weather", llm, make_registry(failing_tool))

        observation = llm.messages[1][-1]["content"]
        self.assertEqual(observation,
                         "Tool error: service unavailable")


class RetryableStreamLLM:
    """Streaming fake whose attempts either raise or yield scripted chunks."""

    def __init__(self, attempts: list[Any]) -> None:
        self.attempts = list(attempts)
        self.calls = 0

    async def chat(self, messages: list[dict[str, Any]],
                   tools: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        raise AssertionError("the runtime should use stream()")

    async def stream(self, messages: list[dict[str, Any]],
                     tools: list[dict[str, Any]] | None = None):
        self.calls += 1
        outcome = self.attempts.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        for chunk in outcome:
            yield chunk


class FlakyChatLLM(FakeLLM):
    """Non-streaming fake that raises once, then serves its responses."""

    def __init__(self, responses: list[dict[str, Any]] | None = None,
                 error: Exception | None = None) -> None:
        super().__init__(responses, error)
        self.calls = 0

    async def chat(self, messages: list[dict[str, Any]],
                   tools: list[dict[str, Any]] | None = None) -> dict[str, Any]:
        self.calls += 1
        if self.error is not None:
            error, self.error = self.error, None
            raise error
        return await super().chat(messages, tools)


def make_retry_harness(**policies: LLMRetryPolicy) -> HarnessSpec:
    entries = {category: LLMRetryPolicy() for category in LLM_RETRY_CATEGORIES}
    entries.update(policies)
    return HarnessSpec(recovery=RecoveryGenome(llm_errors=entries))


class LLMRecoveryTests(unittest.IsolatedAsyncioTestCase):
    ANSWER = [{"content": "It is sunny", "tool_calls": []},
              {"finish_reason": "stop"}]

    async def test_truncated_stream_is_retried_and_recovers(self) -> None:
        llm = RetryableStreamLLM([[{"content": "partial"}], list(self.ANSWER)])
        harness = make_retry_harness(stream_truncated=LLMRetryPolicy(
            max_retries=1, backoff="none", base_delay=0.5))
        trace = RunTrace(run_id="retry-truncated")

        answer = await run_turn("weather", llm, make_registry(), stream=True,
                                harness=harness, trace=trace)

        self.assertEqual(answer, "It is sunny")
        self.assertEqual(llm.calls, 2)
        retries = [event for event in trace.events
                   if event.event_type == "llm.retry"]
        self.assertEqual(len(retries), 1)
        self.assertEqual(retries[0].data["category"], "stream_truncated")
        self.assertEqual(retries[0].data["attempt"], 1)
        self.assertEqual(retries[0].data["max_retries"], 1)

    async def test_empty_stream_is_retried_once(self) -> None:
        llm = RetryableStreamLLM([[{"finish_reason": "stop"}],
                                  list(self.ANSWER)])
        harness = make_retry_harness(stream_empty=LLMRetryPolicy(
            max_retries=1, backoff="none"))

        answer = await run_turn("weather", llm, make_registry(), stream=True,
                                harness=harness)

        self.assertEqual(answer, "It is sunny")
        self.assertEqual(llm.calls, 2)

    async def test_idle_timeout_is_classified_and_retried(self) -> None:
        llm = RetryableStreamLLM([TimeoutError(), list(self.ANSWER)])
        harness = make_retry_harness(stream_idle_timeout=LLMRetryPolicy(
            max_retries=1, backoff="none"))

        answer = await run_turn("weather", llm, make_registry(), stream=True,
                                harness=harness)

        self.assertEqual(answer, "It is sunny")
        self.assertEqual(llm.calls, 2)

    async def test_exhausted_retries_fail_the_turn(self) -> None:
        partial = [{"content": "partial"}]
        llm = RetryableStreamLLM([partial, partial])
        harness = make_retry_harness(stream_truncated=LLMRetryPolicy(
            max_retries=1, backoff="none"))

        answer = await run_turn("weather", llm, make_registry(), stream=True,
                                harness=harness)

        self.assertTrue(answer.startswith("LLM error:"))
        self.assertIn("finish_reason", answer)
        self.assertEqual(llm.calls, 2)

    async def test_default_harness_never_retries_llm_errors(self) -> None:
        llm = RetryableStreamLLM([[{"content": "partial"}],
                                  list(self.ANSWER)])

        answer = await run_turn("weather", llm, make_registry(), stream=True,
                                harness=HarnessSpec())

        self.assertTrue(answer.startswith("LLM error:"))
        self.assertEqual(llm.calls, 1)

    async def test_policies_apply_only_to_their_own_category(self) -> None:
        llm = RetryableStreamLLM([[{"content": "partial"}],
                                  list(self.ANSWER)])
        harness = make_retry_harness(stream_empty=LLMRetryPolicy(
            max_retries=3, backoff="none"))

        answer = await run_turn("weather", llm, make_registry(), stream=True,
                                harness=harness)

        self.assertTrue(answer.startswith("LLM error:"))
        self.assertEqual(llm.calls, 1)

    async def test_provider_error_is_retried_on_non_streaming_path(self) -> None:
        llm = FlakyChatLLM([{"content": "It is sunny", "tool_calls": []}],
                           error=ValueError("provider hiccup"))
        harness = make_retry_harness(provider_error=LLMRetryPolicy(
            max_retries=1, backoff="none"))

        answer = await run_turn("weather", llm, make_registry(),
                                harness=harness)

        self.assertEqual(answer, "It is sunny")
        self.assertEqual(llm.calls, 2)

    async def test_fixed_backoff_sleeps_with_computed_delay(self) -> None:
        llm = RetryableStreamLLM([[{"content": "partial"}],
                                  list(self.ANSWER)])
        harness = make_retry_harness(stream_truncated=LLMRetryPolicy(
            max_retries=1, backoff="fixed", base_delay=0.25))

        with mock.patch.object(chat_module.asyncio, "sleep",
                               new=mock.AsyncMock()) as fake_sleep:
            answer = await run_turn("weather", llm, make_registry(),
                                    stream=True, harness=harness)

        self.assertEqual(answer, "It is sunny")
        fake_sleep.assert_awaited_once_with(0.25)

    async def test_reasoning_budget_bypasses_retry_and_nudges_instead(self) -> None:
        previous = environ.get("AGENT_RUNTIME_MAX_REASONING_CHARS")
        environ["AGENT_RUNTIME_MAX_REASONING_CHARS"] = "10"
        try:
            llm = RetryableStreamLLM([[{"reasoning_content": "y" * 40}],
                                      list(self.ANSWER)])
            # A generous provider_error budget proves the guard never reaches it.
            harness = make_retry_harness(provider_error=LLMRetryPolicy(
                max_retries=5, backoff="none"))

            answer = await run_turn("weather", llm, make_registry(),
                                    stream=True, harness=harness)
        finally:
            if previous is None:
                environ.pop("AGENT_RUNTIME_MAX_REASONING_CHARS", None)
            else:
                environ["AGENT_RUNTIME_MAX_REASONING_CHARS"] = previous

        self.assertEqual(answer, "It is sunny")
        self.assertEqual(llm.calls, 2)


class StreamingConfigTests(unittest.TestCase):
    def test_use_streaming_defaults_to_on(self) -> None:
        previous = environ.pop("AGENT_RUNTIME_STREAM", None)
        try:
            self.assertTrue(use_streaming())
        finally:
            if previous is not None:
                environ["AGENT_RUNTIME_STREAM"] = previous

    def test_use_streaming_reads_env(self) -> None:
        previous = environ.get("AGENT_RUNTIME_STREAM")
        try:
            for raw, expected in [("0", False), ("false", False), ("off", False),
                                  ("1", True), ("yes", True)]:
                environ["AGENT_RUNTIME_STREAM"] = raw
                self.assertIs(use_streaming(), expected)
        finally:
            if previous is None:
                environ.pop("AGENT_RUNTIME_STREAM", None)
            else:
                environ["AGENT_RUNTIME_STREAM"] = previous


class AgentTurnClassTests(unittest.IsolatedAsyncioTestCase):
    async def test_direct_class_run_matches_run_turn_behavior(self) -> None:
        llm = FakeLLM([
            {"content": "", "tool_calls": [{"id": "1", "function": {
                "name": "weather", "arguments": '{"location":"Singapore"}'}}]},
            {"content": "It is sunny", "tool_calls": []},
        ])
        trace = RunTrace(run_id="r-turn")

        answer = await AgentTurn("weather", llm, make_registry(),
                                 trace=trace).run()

        self.assertEqual(answer, "It is sunny")
        self.assertEqual(
            [event.event_type for event in trace.events],
            ["agent.start", "llm.start", "llm.end", "tool.start", "tool.end",
             "llm.start", "llm.end", "agent.end"],
        )

    async def test_class_attributes_bind_harness_defaults(self) -> None:
        llm = FakeLLM([{"content": "hi", "tool_calls": []}])
        turn = AgentTurn("hello", llm, make_registry())

        self.assertEqual(turn.max_iterations, turn.harness.control.max_iterations)
        self.assertIs(turn.trace.harness, turn.harness)
        self.assertIs(turn.events.run_id, turn.trace.run_id)
        await turn.run()

    async def test_class_attributes_honor_overrides(self) -> None:
        llm = FakeLLM([
            {"content": "", "tool_calls": [{"id": "1", "function": {
                "name": "weather", "arguments": '{"location":"Oslo"}'}}]},
            {"content": "summarized", "tool_calls": []},
        ])
        harness = HarnessSpec(control=ControlGenome(max_iterations=1))

        answer = await AgentTurn("weather", llm, make_registry(),
                                 harness=harness).run()

        self.assertEqual(answer, "summarized")
        self.assertEqual(len(llm.messages), 2)


if __name__ == "__main__":
    unittest.main()
