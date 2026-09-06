"""Tool validation, normalization, and observation formatting.

Tool calls from untrusted LLM output are validated against tool schemas
before execution and conversation recording.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from agent_runtime.trace import RunTrace


@dataclass(frozen=True)
class ValidatedToolCall:
    """A tool call that is safe to pass to the executor."""

    id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ToolCallOutcome:
    """Validation verdict for one untrusted LLM tool call.

    ``validated`` and ``rejection`` are mutually exclusive: a call is
    either safe to execute or carries the reason it was rejected.
    """

    tool_call: Any
    validated: ValidatedToolCall | None
    rejection: Exception | None


def _schema_for_tool(schemas: list[dict[str, Any]], name: str) -> Mapping[str, Any] | None:
    for schema in schemas:
        function = schema.get("function")
        if isinstance(function, Mapping) and function.get("name") == name:
            return function
    return None


def _matches_type(value: Any, expected: str) -> bool:
    # bool is a subclass of int, so it needs to be checked separately.
    return {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }.get(expected, True)


def validate_tool_call(tool_call: Any, schemas: list[dict[str, Any]]) -> ValidatedToolCall:
    """Validate and normalize an untrusted LLM tool call."""
    if not isinstance(tool_call, Mapping):
        raise ValueError("tool call must be an object")

    call_id = tool_call.get("id")
    if not isinstance(call_id, str) or not call_id:
        raise ValueError("tool_call_id is required")

    function = tool_call.get("function")
    if not isinstance(function, Mapping):
        raise ValueError("function is required")

    name = function.get("name")
    if not isinstance(name, str) or not name:
        raise ValueError("tool name is required")
    schema = _schema_for_tool(schemas, name)
    if schema is None:
        raise ValueError(f"unknown tool: {name}")

    raw_arguments = function.get("arguments") or "{}"
    if not isinstance(raw_arguments, str):
        raise ValueError("tool arguments must be a JSON string")
    try:
        arguments = json.loads(raw_arguments)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("tool arguments are malformed JSON") from exc
    if not isinstance(arguments, dict):
        raise ValueError("tool arguments must be a JSON object")

    parameters = schema.get("parameters")
    if not isinstance(parameters, Mapping):
        parameters = {}
    properties = parameters.get("properties") or {}
    if not isinstance(properties, Mapping):
        properties = {}
    required = parameters.get("required") or []
    for field in required:
        if field not in arguments:
            raise ValueError(f"missing required argument: {field}")
    for field, value in arguments.items():
        definition = properties.get(field)
        if not isinstance(definition, Mapping):
            if not parameters.get("additionalProperties", True):
                allowed = ", ".join(sorted(properties.keys()))
                raise ValueError(
                    f"unexpected argument '{field}'; "
                    f"allowed arguments: {allowed}. "
                    f"Move '{field}' into the existing argument(s) "
                    "and resend the complete call."
                )
            continue
        expected = definition.get("type")
        if isinstance(expected, str) and not _matches_type(value, expected):
            raise ValueError(f"argument '{field}' must be {expected}")

    return ValidatedToolCall(call_id, name, arguments)


def classify_tool_calls(tool_calls: list[Any],
                        schemas: list[dict[str, Any]]) -> list[ToolCallOutcome]:
    """Validate every tool call up front; nothing raises."""
    outcomes: list[ToolCallOutcome] = []
    for tool_call in tool_calls:
        try:
            validated = validate_tool_call(tool_call, schemas)
        except Exception as exc:
            outcomes.append(ToolCallOutcome(tool_call, None, exc))
        else:
            outcomes.append(ToolCallOutcome(tool_call, validated, None))
    return outcomes


def _canonical_tool_call(validated: ValidatedToolCall) -> dict[str, Any]:
    """Serialize a validated call so history only ever holds valid JSON."""
    return {"id": validated.id, "type": "function",
            "function": {"name": validated.name,
                         "arguments": json.dumps(validated.arguments)}}


def canonical_tool_calls(outcomes: list[ToolCallOutcome]) -> list[dict[str, Any]]:
    """The tool calls allowed into the conversation history, canonicalized."""
    return [_canonical_tool_call(outcome.validated)
            for outcome in outcomes if outcome.validated is not None]


def _tool_observation_message(tool_call: Any, content: str,
                              metadata: dict[str, Any] | None = None
                              ) -> dict[str, Any]:
    """Build the observation message for a tool call's outcome.

    ``metadata`` carries programmatic side-channel data (e.g. a spill
    path for the memory strategies); it never enters the model-facing
    ``content``.
    """
    call_id = tool_call.get("id", "") if isinstance(tool_call, Mapping) else ""
    if isinstance(call_id, str) and call_id:
        message: dict[str, Any] = {"role": "tool", "tool_call_id": call_id,
                                   "content": content}
        if metadata:
            message["metadata"] = metadata
        return message
    # The model must retry because a tool message cannot be correlated without an ID.
    return {
        "role": "user",
        "content": f"{content} Please retry this tool call with a valid tool_call_id.",
    }


def _rejected_observation_content(tool_call: Any, observation: str) -> str:
    """Name the rejected tool so the model can retry the corrected call."""
    tool = _tool_call_payload(tool_call).get("tool")
    name = tool if isinstance(tool, str) and tool else "an unknown tool"
    return f"Tool call to {name} was rejected and not executed. {observation}"


def _rejected_observation_message(tool_call: Any, content: str) -> dict[str, Any]:
    """Build the observation message for a tool call rejected before execution.

    The rejected call never enters the conversation history, so the observation
    must be a user message: a ``role: "tool"`` message would reference a
    tool_call_id that no assistant message carries.
    """
    call_id = tool_call.get("id") if isinstance(tool_call, Mapping) else None
    if isinstance(call_id, str) and call_id:
        retry = "Please retry this tool call with valid JSON arguments."
    else:
        retry = "Please retry this tool call with a valid tool_call_id."
    return {"role": "user", "content": f"{content} {retry}"}


def _trace_tool_rejection(trace: RunTrace, iteration: int,
                          tool_meta: dict[str, Any], error: Exception) -> None:
    """Record a rejected tool call with the normal tool span lifecycle."""
    trace.emit("tool.start", iteration, **tool_meta)
    error_meta = dict(tool_meta, error=str(error), duration_ms=0.0)
    trace.emit("tool.error", iteration, **error_meta)
    end_meta = dict(tool_meta, duration_ms=0.0, status="error")
    trace.emit("tool.end", iteration, **end_meta)


def _tool_trace_metadata(tool_call: Any) -> dict[str, Any]:
    """Return identifiers without copying the model's full tool payload."""
    if not isinstance(tool_call, Mapping):
        return {}
    function = tool_call.get("function")
    return {
        "tool_call_id": tool_call.get("id"),
        "tool": function.get("name") if isinstance(function, Mapping) else None,
    }


def _tail_text(text: Any, limit: int = 2000) -> str:
    """Return the trailing part of a tool result for user-facing events."""
    value = text if isinstance(text, str) else ("" if text is None else str(text))
    value = value.rstrip()
    if len(value) <= limit:
        return value
    return "..." + value[-limit:]


def _tool_call_payload(tool_call: Any) -> dict[str, Any]:
    """Best-effort user-facing view of an untrusted tool call."""
    if not isinstance(tool_call, Mapping):
        return {"tool": None, "call_id": None, "arguments": str(tool_call)}
    function = tool_call.get("function")
    name = raw_arguments = None
    if isinstance(function, Mapping):
        name = function.get("name")
        raw_arguments = function.get("arguments")
    arguments: Any = raw_arguments
    if isinstance(raw_arguments, str) and raw_arguments:
        try:
            arguments = json.loads(raw_arguments)
        except json.JSONDecodeError:
            arguments = raw_arguments
    return {"tool": name, "call_id": tool_call.get("id"), "arguments": arguments}


def _tool_result_summary(result: Any) -> dict[str, Any]:
    """Summarize a tool result for user-facing events."""
    if isinstance(result, Mapping) and "exit_code" in result and "stdout" in result:
        summary: dict[str, Any] = {
            "exit_code": result.get("exit_code"),
            "stdout_tail": _tail_text(result.get("stdout")),
            "stderr_tail": _tail_text(result.get("stderr")),
        }
        error = result.get("error")
        if isinstance(error, Mapping):
            summary["error"] = f"{error.get('type', 'Error')}: {error.get('message', '')}"
        return summary
    return {"result": _tail_text(result)}


__all__ = [
    "ToolCallOutcome",
    "ValidatedToolCall",
    "canonical_tool_calls",
    "classify_tool_calls",
    "validate_tool_call",
]
