"""Provider-specific tool-call extras must survive into the history.

Gemini's OpenAI-compatible endpoint attaches ``extra_content.google.
thought_signature`` to each ``functionCall`` part and rejects the next
request with ``400 INVALID_ARGUMENT ... missing a thought_signature`` when
it is not echoed back. The runtime treats these fields as opaque payload:
it never reads or rewrites them, it only keeps them attached to the call.
"""

import json
import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).parents[2] / "src"))

from agent_runtime.agent.llm_stream import (
    consume_stream,
    merge_stream_tool_call,
)
from agent_runtime.agent.tool_dispatch import (
    canonical_tool_calls,
    classify_tool_calls,
    tool_trace_metadata,
    validate_tool_call,
)
from agent_runtime.execution.local import LocalWorkspace
from agent_runtime.tools.tools import builtin_tool_specs

SIGNATURE = {"google": {"thought_signature": "SIG-opaque-value"}}


def _schemas() -> list[dict]:
    workspace = LocalWorkspace()
    return [spec.schema for spec in builtin_tool_specs(workspace=workspace)]


def _stream_delta(call_id: str, path: str, *,
                  signature: dict | None = None) -> dict:
    """One delta shaped like the provider's: index=None drops out entirely."""
    delta = {"id": call_id, "type": "function",
             "function": {"name": "read_file",
                          "arguments": json.dumps({"path": path})}}
    if signature is not None:
        delta["extra_content"] = signature
    return delta


class _FakeStreamLLM:
    """Replays canned deltas, then a tool_calls finish marker."""

    def __init__(self, deltas: list[dict]) -> None:
        self.deltas = deltas

    def stream(self, messages, tools=None):
        async def _chunks():
            for delta in self.deltas:
                yield {"content": "", "reasoning_content": "",
                       "tool_calls": [delta]}
            yield {"content": "", "reasoning_content": "",
                   "finish_reason": "tool_calls"}
        return _chunks()


class MergeStreamToolCallTests(unittest.TestCase):
    def test_extras_are_kept_and_index_is_not(self) -> None:
        calls: list[dict] = []
        merge_stream_tool_call(calls, {"index": 0, **_stream_delta(
            "call_a", "a.txt", signature=SIGNATURE)})

        self.assertEqual(calls[0]["extra_content"], SIGNATURE)
        self.assertNotIn("index", calls[0])
        self.assertEqual(calls[0]["id"], "call_a")

    def test_an_unsigned_call_in_the_same_batch_stays_unsigned(self) -> None:
        calls: list[dict] = []
        merge_stream_tool_call(calls, {"index": 0, **_stream_delta(
            "call_a", "a.txt", signature=SIGNATURE)})
        merge_stream_tool_call(calls, {"index": 1,
                                        **_stream_delta("call_b", "b.txt")})

        self.assertEqual(calls[0]["extra_content"], SIGNATURE)
        self.assertNotIn("extra_content", calls[1])
        self.assertEqual([call["id"] for call in calls], ["call_a", "call_b"])

    def test_a_later_argument_delta_does_not_drop_the_signature(self) -> None:
        calls: list[dict] = []
        merge_stream_tool_call(calls, {"index": 0, "id": "call_a",
                                        "function": {"name": "read_file"},
                                        "extra_content": SIGNATURE})
        merge_stream_tool_call(calls, {"index": 0,
                                        "function": {"arguments": '{"x": 1}'}})

        self.assertEqual(calls[0]["extra_content"], SIGNATURE)
        self.assertEqual(calls[0]["function"]["arguments"], '{"x": 1}')


class StreamedExtrasTests(unittest.IsolatedAsyncioTestCase):
    async def test_extras_reach_the_assembled_response(self) -> None:
        deltas = [_stream_delta("call_a", "a.txt", signature=SIGNATURE),
                  _stream_delta("call_b", "b.txt")]

        response = await consume_stream(_FakeStreamLLM(deltas), [], None, 1)

        calls = response["tool_calls"]
        self.assertEqual(calls[0]["extra_content"], SIGNATURE)
        self.assertNotIn("extra_content", calls[1])


class CanonicalHistoryTests(unittest.TestCase):
    def test_signature_survives_into_the_history_entry(self) -> None:
        raw = {"id": "call_a", "type": "function",
               "function": {"name": "read_file",
                            "arguments": json.dumps({"path": "a.txt"})},
               "extra_content": SIGNATURE}

        (outcome,) = classify_tool_calls([raw], _schemas())
        (canonical,) = canonical_tool_calls([outcome])

        self.assertEqual(canonical["extra_content"], SIGNATURE)
        self.assertEqual(canonical["id"], "call_a")
        self.assertEqual(canonical["function"]["arguments"],
                         json.dumps({"path": "a.txt"}))

    def test_extras_never_override_the_runtime_owned_wire_keys(self) -> None:
        raw = {"id": "call_a", "type": "function",
               "function": {"name": "read_file",
                            "arguments": json.dumps({"path": "a.txt"})},
               # A provider echoing these back must not rewrite the call.
               "index": 3}

        validated = validate_tool_call(raw, _schemas())

        self.assertEqual(validated.extras, {})
        self.assertEqual(validated.id, "call_a")

    def test_a_rejected_call_contributes_no_history_entry(self) -> None:
        raw = {"id": "call_a", "type": "function",
               "function": {"name": "read_file", "arguments": "not json"},
               "extra_content": SIGNATURE}

        (outcome,) = classify_tool_calls([raw], _schemas())

        self.assertIsNotNone(outcome.rejection)
        self.assertEqual(canonical_tool_calls([outcome]), [])

    def test_extras_stay_out_of_trace_and_user_facing_views(self) -> None:
        raw = {"id": "call_a", "type": "function",
               "function": {"name": "read_file",
                            "arguments": json.dumps({"path": "a.txt"})},
               "extra_content": {"google": {"thought_signature": "S" * 512}}}

        (outcome,) = classify_tool_calls([raw], _schemas())
        (canonical,) = canonical_tool_calls([outcome])

        metadata = tool_trace_metadata(canonical)
        self.assertNotIn("S" * 512, json.dumps(metadata))
        self.assertEqual(metadata["tool"], "read_file")


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
