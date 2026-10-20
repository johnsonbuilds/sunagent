"""Render agent events as web UI frames for browser clients.

The browser is a channel like any other: it consumes the same
provider-independent events as the CLI and decides on its own how to
present them. This module owns one thing only -- the translation from
:class:`~agent_runtime.events.AgentEvent` to plain JSON-serializable
frames. It performs no I/O of its own beyond handing finished frames to
the injected sink.

Frame vocabulary (``id`` ties a live frame to its finalized segment):

    run.started      {run_id, message}
    delta            {id, channel, text}      channel is assistant|reasoning
    tool.started     {id, call_id, tool, summary, arguments}
    tool.completed   {id, call_id, tool, status, duration, output, exit_code}
    tool.failed      {id, call_id, tool, status, error}
    segment          {segment}                a finished transcript block
    run.completed    {answer, iterations}
    run.error        {stage, error}

A ``segment`` frame is the authoritative, persistable form of a block.
The browser upserts blocks by ``id``, so live frames and replayed
history converge on the same DOM without the client tracking two code
paths.
"""

from __future__ import annotations

import json
import os
from typing import Any, Callable

from agent_runtime.events import AgentEvent


FRAME_RUN_STARTED = "run.started"
FRAME_DELTA = "delta"
FRAME_TOOL_STARTED = "tool.started"
FRAME_TOOL_COMPLETED = "tool.completed"
FRAME_TOOL_FAILED = "tool.failed"
FRAME_SEGMENT = "segment"
FRAME_RUN_COMPLETED = "run.completed"
FRAME_RUN_ERROR = "run.error"


def _reasoning_enabled_by_default() -> bool:
    return os.getenv("AGENT_RUNTIME_WEBUI_REASONING", "1") != "0"


class WebUIChannel:
    """Channel adapter that turns agent events into web UI frames.

    The channel owns presentation decisions (which blocks exist, how a
    tool call is summarised, when a block is final) but never touches
    the network: frames are pushed to the injected sink, which is
    expected to be non-blocking.

    Unknown event types are ignored so newer runtimes keep working with
    older channels.
    """

    def __init__(self, *, sink: Callable[[dict[str, Any]], None],
                 show_reasoning: bool | None = None) -> None:
        self._sink = sink
        self._show_reasoning = (_reasoning_enabled_by_default()
                                if show_reasoning is None else show_reasoning)
        self._run_id = ""
        self._counter = 0
        self._pending: list[dict[str, Any]] = []
        self._segments: list[dict[str, Any]] = []
        self._tools: dict[str, dict[str, Any]] = {}

    # -- channel interface ------------------------------------------------

    def __call__(self, event: AgentEvent) -> None:
        handler = getattr(self, f"_on_{event.event_type.replace('.', '_')}", None)
        if handler is not None:
            handler(event)

    def close(self) -> None:
        """Flush any unfinished streaming block (safe to call more than once)."""
        self._flush_pending()

    @property
    def run_id(self) -> str:
        return self._run_id

    @property
    def segments(self) -> list[dict[str, Any]]:
        """The finalized blocks produced so far, in transcript order."""
        return [dict(block) for block in self._segments]

    # -- handlers ---------------------------------------------------------

    def _on_agent_started(self, event: AgentEvent) -> None:
        self._run_id = event.run_id
        self._counter = 0
        self._pending = []
        self._segments = []
        self._tools = {}
        self._emit({"type": FRAME_RUN_STARTED, "run_id": event.run_id,
                    "message": event.data.get("message") or ""})

    def _on_assistant_started(self, event: AgentEvent) -> None:
        self._pending = []

    def _on_assistant_delta(self, event: AgentEvent) -> None:
        reasoning = event.data.get("reasoning") or ""
        content = event.data.get("content") or ""
        # Reasoning is emitted before content: reasoning models stream
        # the chain of thought ahead of the visible answer in the same
        # chunk, and the browser renders them as separate blocks.
        if reasoning and self._show_reasoning:
            self._append_text("reasoning", reasoning)
        if content:
            self._append_text("assistant", content)

    def _on_assistant_completed(self, event: AgentEvent) -> None:
        self._flush_pending()

    def _on_tool_started(self, event: AgentEvent) -> None:
        call_id = event.call_id or event.event_id
        tool = event.data.get("tool") or "tool"
        arguments = event.data.get("arguments")
        if not isinstance(arguments, dict):
            arguments = {}
        summary = describe_tool(tool, arguments)
        self._tools[call_id] = {"tool": tool, "arguments": arguments,
                                "summary": summary}
        self._emit({"type": FRAME_TOOL_STARTED, "id": self._tool_id(call_id),
                    "call_id": call_id, "tool": tool,
                    "summary": summary,
                    "arguments": arguments})

    def _on_tool_completed(self, event: AgentEvent) -> None:
        call_id = event.call_id or event.event_id
        data = event.data
        known = self._tools.get(call_id, {})
        exit_code = data.get("exit_code")
        failed = bool(data.get("error")) or (exit_code is not None and exit_code != 0)
        duration = data.get("duration")
        segment: dict[str, Any] = {
            "id": self._tool_id(call_id),
            "type": "tool",
            "call_id": call_id,
            "tool": known.get("tool") or data.get("tool") or "tool",
            "summary": known.get("summary") or "",
            "arguments": known.get("arguments") or {},
            "status": "error" if failed else "ok",
            "duration": duration if isinstance(duration, (int, float)) else None,
            "exit_code": exit_code if isinstance(exit_code, int) else None,
            "output": _tool_output(data),
        }
        if failed and data.get("error"):
            segment["error"] = str(data["error"])
        self._segments.append(segment)
        self._emit(_tool_frame(FRAME_TOOL_COMPLETED, segment))
        self._emit({"type": FRAME_SEGMENT, "segment": segment})

    def _on_tool_failed(self, event: AgentEvent) -> None:
        call_id = event.call_id or event.event_id
        known = self._tools.get(call_id, {})
        segment: dict[str, Any] = {
            "id": self._tool_id(call_id),
            "type": "tool",
            "call_id": call_id,
            "tool": known.get("tool") or event.data.get("tool") or "tool",
            "summary": known.get("summary") or "",
            "arguments": known.get("arguments") or {},
            "status": "error",
            "duration": None,
            "exit_code": None,
            "output": "",
            "error": str(event.data.get("error") or "failed"),
        }
        self._segments.append(segment)
        self._emit(_tool_frame(FRAME_TOOL_FAILED, segment))
        self._emit({"type": FRAME_SEGMENT, "segment": segment})

    def _on_agent_completed(self, event: AgentEvent) -> None:
        self._flush_pending()
        self._emit({"type": FRAME_RUN_COMPLETED,
                    "answer": event.data.get("answer") or "",
                    "iterations": event.data.get("iterations")})

    def _on_runtime_error(self, event: AgentEvent) -> None:
        self._emit({"type": FRAME_RUN_ERROR,
                    "stage": event.data.get("stage") or "runtime",
                    "error": event.data.get("error") or "unknown error"})

    # -- internals --------------------------------------------------------

    def _append_text(self, kind: str, text: str) -> None:
        if not text:
            return
        # Consecutive deltas of the same kind extend the current block;
        # a switch of kind starts a new one so interleaved reasoning and
        # content keep their relative order in the transcript.
        if self._pending and self._pending[-1]["type"] == kind:
            block = self._pending[-1]
            block["text"] += text
        else:
            block = {"id": self._next_id(kind), "type": kind, "text": text}
            self._pending.append(block)
        self._emit({"type": FRAME_DELTA, "id": block["id"],
                    "channel": kind, "text": text})

    def _flush_pending(self) -> None:
        for block in self._pending:
            segment = {"id": block["id"], "type": block["type"],
                       "content": block["text"]}
            self._segments.append(segment)
            self._emit({"type": FRAME_SEGMENT, "segment": segment})
        self._pending = []

    def _next_id(self, kind: str) -> str:
        self._counter += 1
        return f"{self._run_id}:{kind}:{self._counter}"

    def _tool_id(self, call_id: str) -> str:
        return f"{self._run_id}:tool:{call_id}"

    def _emit(self, frame: dict[str, Any]) -> None:
        self._sink(frame)


def _tool_frame(frame_type: str, segment: dict[str, Any]) -> dict[str, Any]:
    """Flatten a tool segment into a live frame.

    The segment carries its own ``type`` (``tool``) which would shadow
    the frame type, so the frame type is applied last.
    """
    frame = {key: value for key, value in segment.items() if key != "type"}
    frame["type"] = frame_type
    return frame


def describe_tool(tool: str, arguments: dict[str, Any]) -> str:
    """Summarise a tool call in one short human-readable line."""
    if tool == "run_command":
        command = arguments.get("command")
        if isinstance(command, str):
            return f"$ {command}"
    if tool == "execute_code":
        code = arguments.get("code")
        language = arguments.get("language") or "python"
        if isinstance(code, str):
            first = next((line.strip() for line in code.splitlines()
                          if line.strip()), "")
            if len(first) > 80:
                first = first[:79] + "…"
            return f"$ {language} · {first}"
    if tool == "write_file":
        content = arguments.get("content")
        size = len(content) if isinstance(content, str) else 0
        return f"write {arguments.get('path')} ({size} chars)"
    if tool == "grep_search":
        return f"grep {arguments.get('pattern')}"
    if tool == "find_files":
        return f"find {arguments.get('pattern')}"
    if tool == "find_symbol":
        return f"symbol {arguments.get('name')}"
    if tool == "find_references":
        return f"refs {arguments.get('name')}"
    if tool == "apply_patch":
        patch = arguments.get("patch")
        if isinstance(patch, str):
            return f"patch: {patch.count('>>>>>>> REPLACE')} edit(s)"
    if not arguments:
        return ""
    return json.dumps(arguments, ensure_ascii=False, sort_keys=True)


def _tool_output(data: dict[str, Any]) -> str:
    """Extract the human-visible output of a completed tool call."""
    if "exit_code" in data:
        parts = []
        stdout = data.get("stdout_tail")
        if isinstance(stdout, str) and stdout.strip():
            parts.append(stdout.rstrip())
        stderr = data.get("stderr_tail")
        failed = data.get("exit_code") not in (None, 0) or bool(data.get("error"))
        if failed and isinstance(stderr, str) and stderr.strip():
            parts.append(stderr.rstrip())
        return "\n".join(parts)
    result = data.get("result")
    if isinstance(result, str):
        return result
    if result is None:
        return ""
    return json.dumps(result, ensure_ascii=False, default=str)


__all__ = ["FRAME_DELTA", "FRAME_RUN_COMPLETED", "FRAME_RUN_ERROR",
           "FRAME_RUN_STARTED", "FRAME_SEGMENT", "FRAME_TOOL_COMPLETED",
           "FRAME_TOOL_FAILED", "FRAME_TOOL_STARTED", "WebUIChannel",
           "describe_tool"]
