"""Render agent events as incremental terminal output."""

from __future__ import annotations

import json
import os
import sys
from typing import Any, TextIO

from agent_runtime.events import AgentEvent


_DIM, _RESET = "\x1b[2m", "\x1b[0m"
_GREEN, _RED = "\x1b[32m", "\x1b[31m"

_STDOUT_LINES = 5
_RESULT_LINES = 3
_LINE_WIDTH = 100


def _reasoning_enabled_by_default() -> bool:
    return os.getenv("AGENT_RUNTIME_CLI_REASONING", "1") != "0"


class CLIRenderer:
    """Channel adapter that turns agent events into terminal output.

    Rendering is purely local: the renderer receives the same
    provider-independent events as any other channel and decides on its
    own how to present them. Unknown event types are ignored so newer
    runtimes keep working with older channels.
    """

    def __init__(self, stream: TextIO | None = None, *,
                 color: bool | None = None,
                 show_reasoning: bool | None = None) -> None:
        self._stream = stream or sys.stdout
        self._color = self._stream.isatty() if color is None else color
        self._show_reasoning = (_reasoning_enabled_by_default()
                                if show_reasoning is None else show_reasoning)
        self._line_open = False
        self._mode: str | None = None

    def __call__(self, event: AgentEvent) -> None:
        handler = getattr(self, f"_on_{event.event_type.replace('.', '_')}", None)
        if handler is not None:
            handler(event)

    def _write(self, text: str) -> None:
        print(text, file=self._stream, end="", flush=True)

    def _paint(self, text: str, *codes: str) -> str:
        if not self._color:
            return text
        return "".join(codes) + text + _RESET

    def _close_line(self) -> None:
        if self._line_open:
            self._write("\n")
        self._line_open = False
        self._mode = None

    def _on_agent_completed(self, event: AgentEvent) -> None:
        self._close_line()
        self._write("\n")

    def _on_assistant_completed(self, event: AgentEvent) -> None:
        self._close_line()

    def _on_assistant_delta(self, event: AgentEvent) -> None:
        content = event.data.get("content") or ""
        reasoning = event.data.get("reasoning") or ""
        if reasoning and self._show_reasoning:
            if self._mode != "reasoning":
                self._close_line()
                self._mode = "reasoning"
                self._line_open = True
                self._write(self._paint("▌ ", _DIM))
            self._write(self._paint(reasoning, _DIM))
        if content:
            if self._mode != "content":
                self._close_line()
                self._mode = "content"
            self._line_open = True
            self._write(content)

    def _on_tool_started(self, event: AgentEvent) -> None:
        self._close_line()
        self._write(f"● {event.data.get('tool') or 'tool'}\n")
        arguments = self._format_arguments(event)
        if arguments:
            self._write(f"  {arguments}\n")

    def _on_tool_completed(self, event: AgentEvent) -> None:
        self._close_line()
        data = event.data
        if "exit_code" in data:
            for line in self._tail_lines(data.get("stdout_tail"), _STDOUT_LINES):
                self._write(self._paint(f"  │ {line}\n", _DIM))
            failed = data.get("exit_code") != 0 or bool(data.get("error"))
            if failed:
                for line in self._tail_lines(data.get("stderr_tail"), _STDOUT_LINES):
                    self._write(self._paint(f"  │ {line}\n", _DIM))
            if data.get("exit_code") is None:
                status = self._paint(f"✗ {data.get('error') or 'failed'}", _RED)
            elif failed:
                status = self._paint("✗", _RED) + f" exit {data['exit_code']}"
            else:
                status = self._paint("✓", _GREEN) + f" exit {data['exit_code']}"
        else:
            for line in self._tail_lines(data.get("result"), _RESULT_LINES):
                self._write(self._paint(f"  │ {line}\n", _DIM))
            status = self._paint("✓", _GREEN) + " done"
        duration = data.get("duration")
        if isinstance(duration, (int, float)):
            status += f" · {duration:.1f}s"
        self._write(f"  {status}\n")

    def _on_tool_failed(self, event: AgentEvent) -> None:
        self._close_line()
        error = event.data.get("error") or "failed"
        self._write(f"  {self._paint('✗', _RED)} {error}\n")

    def _on_runtime_error(self, event: AgentEvent) -> None:
        self._close_line()
        stage = event.data.get("stage") or "runtime"
        error = event.data.get("error") or "unknown error"
        self._write(self._paint(f"⚠ {stage}: {error}\n", _RED))
        self._write("\n")

    @staticmethod
    def _format_arguments(event: AgentEvent) -> str:
        arguments = event.data.get("arguments")
        tool = event.data.get("tool")
        if (tool == "run_command" and isinstance(arguments, dict)
                and isinstance(arguments.get("command"), str)):
            return f"$ {arguments['command']}"
        if (tool == "execute_code" and isinstance(arguments, dict)
                and isinstance(arguments.get("code"), str)):
            language = arguments.get("language") or "python"
            first_line = next((line.strip() for line in arguments["code"].splitlines()
                               if line.strip()), "")
            preview = first_line if len(first_line) <= 80 else first_line[:79] + "…"
            return f"$ {language} · {preview}"
        if (tool == "write_file" and isinstance(arguments, dict)):
            content = arguments.get("content")
            size = len(content) if isinstance(content, str) else 0
            return f"write {arguments.get('path')} ({size} chars)"
        if (tool == "grep_search" and isinstance(arguments, dict)):
            pattern = arguments.get("pattern")
            return f"$ grep {pattern}"
        if (tool == "find_files" and isinstance(arguments, dict)):
            return f"$ find {arguments.get('pattern')}"
        if (tool == "find_symbol" and isinstance(arguments, dict)):
            return f"$ symbol {arguments.get('name')}"
        if (tool == "find_references" and isinstance(arguments, dict)):
            return f"$ refs {arguments.get('name')}"
        if (tool == "apply_patch" and isinstance(arguments, dict)
                and isinstance(arguments.get("patch"), str)):
            blocks = arguments["patch"].count(">>>>>>> REPLACE")
            return f"patch: {blocks} edit(s)"
        if arguments is None:
            return ""
        return json.dumps(arguments, ensure_ascii=False, sort_keys=True)

    @staticmethod
    def _tail_lines(text: Any, limit: int) -> list[str]:
        value = text if isinstance(text, str) else ("" if text is None else str(text))
        lines = [line.strip() for line in value.splitlines()]
        lines = [line for line in lines if line]
        lines = lines[-limit:]
        return [line if len(line) <= _LINE_WIDTH else line[:_LINE_WIDTH - 1] + "…"
                for line in lines]


__all__ = ["CLIRenderer"]
