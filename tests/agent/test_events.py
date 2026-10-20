import asyncio
import logging
import sys
import unittest
from pathlib import Path
from typing import Any


sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from agent_runtime.agent.loop import run_turn
from agent_runtime.events import AgentEvent, EventEmitter
from agent_runtime.harness import HarnessSpec
from agent_runtime.tools.tools import ToolRegistry, ToolSpec


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

    async def stream(self, messages: list[dict[str, Any]],
                     tools: list[dict[str, Any]] | None = None):
        self.messages.append(messages.copy())
        for chunk in self.responses.pop(0):
            yield chunk


async def default_handler(location: str) -> str:
    return location


def make_registry(handler: Any = default_handler) -> ToolRegistry:
    return ToolRegistry([ToolSpec("weather", "", WEATHER_SCHEMA, handler)])


class UserEventSequenceTests(unittest.IsolatedAsyncioTestCase):
    async def test_streaming_run_emits_user_event_sequence(self) -> None:
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
        events = EventEmitter(run_id="r1")

        self.assertEqual(await run_turn("weather", llm, make_registry(), stream=True,
                                        events=events),
                         "It is sunny")
        self.assertEqual(
            [event.event_type for event in events.events],
            ["agent.started",
             "assistant.started",
             "assistant.delta", "assistant.delta", "assistant.delta",
             "assistant.completed",
             "tool.started", "tool.completed",
             "assistant.started",
             "assistant.delta",
             "assistant.completed",
             "agent.completed"],
        )
        self.assertEqual({event.run_id for event in events.events}, {"r1"})
        self.assertEqual(events.events[0].data, {"message": "weather"})
        self.assertEqual(events.events[2].data,
                         {"content": "", "reasoning": "thinking about weather"})
        self.assertEqual(events.events[3].data, {"content": "Checking ", "reasoning": ""})
        completed_rounds = [event for event in events.events
                            if event.event_type == "assistant.completed"]
        self.assertEqual(completed_rounds[0].data, {"tool_calls": ["weather"]})
        self.assertEqual(completed_rounds[1].data, {"tool_calls": []})
        tool_started = next(event for event in events.events
                            if event.event_type == "tool.started")
        self.assertEqual(tool_started.call_id, "1")
        self.assertEqual(tool_started.data["tool"], "weather")
        self.assertEqual(tool_started.data["arguments"], {"location": "Singapore"})
        tool_completed = next(event for event in events.events
                              if event.event_type == "tool.completed")
        self.assertEqual(tool_completed.call_id, "1")
        self.assertEqual(tool_completed.data["result"], "Singapore")
        self.assertIn("duration", tool_completed.data)
        agent_completed = events.events[-1]
        self.assertEqual(agent_completed.iteration, 2)
        self.assertEqual(agent_completed.data,
                         {"iterations": 2, "answer": "It is sunny"})

    async def test_non_streaming_run_emits_one_delta_per_round(self) -> None:
        llm = FakeLLM([
            {"content": "", "tool_calls": [{"id": "1", "function": {
                "name": "weather", "arguments": '{"location":"Singapore"}'}}]},
            {"content": "It is sunny", "tool_calls": []},
        ])
        events = EventEmitter(run_id="r1")

        self.assertEqual(await run_turn("weather", llm, make_registry(), events=events),
                         "It is sunny")
        deltas = [event for event in events.events
                  if event.event_type == "assistant.delta"]
        self.assertEqual(len(deltas), 1)
        self.assertEqual(deltas[0].data, {"content": "It is sunny", "reasoning": ""})

    async def test_shell_tool_result_is_summarized(self) -> None:
        async def shell_tool(command: str) -> dict[str, Any]:
            return {"stdout": "test_a ..\ntest_b ..", "stderr": "AssertionError: boom",
                    "exit_code": 1, "duration": 2.5}

        schema = {"type": "object", "properties": {"command": {"type": "string"}},
                  "required": ["command"]}
        registry = ToolRegistry([ToolSpec("run_command", "", schema, shell_tool)])
        llm = FakeLLM([
            {"content": "", "tool_calls": [{"id": "c1", "function": {
                "name": "run_command", "arguments": '{"command":"pytest"}'}}]},
            {"content": "done", "tool_calls": []},
        ])
        events = EventEmitter(run_id="r1")

        self.assertEqual(await run_turn("run tests", llm, registry, events=events), "done")
        tool_completed = next(event for event in events.events
                              if event.event_type == "tool.completed")
        self.assertEqual(tool_completed.call_id, "c1")
        self.assertEqual(tool_completed.data["exit_code"], 1)
        self.assertEqual(tool_completed.data["stdout_tail"], "test_a ..\ntest_b ..")
        self.assertEqual(tool_completed.data["stderr_tail"], "AssertionError: boom")
        self.assertNotIn("result", tool_completed.data)

    async def test_tool_exception_emits_tool_failed(self) -> None:
        async def failing_tool(location: str) -> str:
            raise RuntimeError("service unavailable")

        llm = FakeLLM([
            {"content": "", "tool_calls": [{"id": "1", "function": {
                "name": "weather", "arguments": '{"location":"Paris"}'}}]},
            {"content": "fallback", "tool_calls": []},
        ])
        events = EventEmitter(run_id="r1")

        self.assertEqual(
            await run_turn("weather", llm, make_registry(failing_tool), events=events),
            "fallback")
        tool_failed = next(event for event in events.events
                           if event.event_type == "tool.failed")
        self.assertEqual(tool_failed.call_id, "1")
        self.assertEqual(tool_failed.data["tool"], "weather")
        self.assertEqual(tool_failed.data["error"], "service unavailable")

    async def test_unknown_tool_emits_started_then_failed(self) -> None:
        llm = FakeLLM([
            {"content": "", "tool_calls": [{"id": "unknown", "function": {
                "name": "missing", "arguments": "{}"}}]},
            {"content": "recovered", "tool_calls": []},
        ])
        events = EventEmitter(run_id="r1")

        self.assertEqual(await run_turn("weather", llm, make_registry(), events=events),
                         "recovered")
        tool_events = [event for event in events.events
                       if event.event_type.startswith("tool.")]
        self.assertEqual([event.event_type for event in tool_events],
                         ["tool.started", "tool.failed"])
        self.assertEqual(tool_events[0].data["tool"], "missing")
        self.assertEqual(tool_events[0].data["arguments"], {})
        self.assertIn("unknown tool", tool_events[1].data["error"])

    async def test_llm_error_emits_runtime_error(self) -> None:
        events = EventEmitter(run_id="r1")

        self.assertEqual(
            await run_turn("hello", FakeLLM(error=RuntimeError("network down")),
                           make_registry(), events=events,
                           harness=HarnessSpec()),
            "LLM error: network down")
        self.assertEqual([event.event_type for event in events.events],
                         ["agent.started", "assistant.started", "runtime.error"])
        runtime_error = events.events[-1]
        self.assertEqual(runtime_error.data,
                         {"stage": "llm", "error": "LLM error: network down"})

    async def test_failing_subscriber_does_not_break_the_run(self) -> None:
        events = EventEmitter()
        events.subscribe(lambda event: 1 / 0)
        received: list[AgentEvent] = []
        events.subscribe(received.append)
        llm = FakeLLM([{"content": "answer", "tool_calls": []}])

        logging.disable(logging.CRITICAL)
        try:
            self.assertEqual(
                await run_turn("hello", llm, make_registry(), events=events), "answer")
        finally:
            logging.disable(logging.NOTSET)
        self.assertEqual(received[-1].event_type, "agent.completed")
        self.assertEqual(received[-1].data["answer"], "answer")


class EventEmitterTests(unittest.IsolatedAsyncioTestCase):
    async def test_stream_iterator_yields_events_until_closed(self) -> None:
        events = EventEmitter(run_id="r1")
        collected: list[AgentEvent] = []

        async def consume() -> None:
            async for event in events.stream():
                collected.append(event)

        task = asyncio.create_task(consume())
        await asyncio.sleep(0)
        events.emit("agent.started", message="hi")
        events.close()
        await task

        self.assertEqual([event.event_type for event in collected], ["agent.started"])
        self.assertEqual(collected[0].data, {"message": "hi"})

    async def test_unsubscribe_stops_delivery(self) -> None:
        events = EventEmitter(run_id="r1")
        received: list[AgentEvent] = []
        unsubscribe = events.subscribe(received.append)

        events.emit("agent.started", message="hi")
        unsubscribe()
        events.emit("agent.completed", iterations=1, answer="hi")

        self.assertEqual([event.event_type for event in received], ["agent.started"])

    def test_agent_event_to_dict(self) -> None:
        event = AgentEvent("r1", "e1", "tool.started", 1.0, 2, "call-1",
                           {"tool": "weather"})
        self.assertEqual(event.to_dict(), {
            "run_id": "r1",
            "event_id": "e1",
            "event_type": "tool.started",
            "timestamp": 1.0,
            "iteration": 2,
            "call_id": "call-1",
            "data": {"tool": "weather"},
        })


if __name__ == "__main__":
    unittest.main()
