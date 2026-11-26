"""Compact observation rendering with spill-at-birth archiving.

Every tool result is re-sent on every iteration, so the transcript is the
scarce resource.  Invariant: any observation that compaction could ever
truncate is archived at birth under ``.outputs/obs-<tool_call_id>.txt``.
Shorter ones can never lose data, so they skip the archive (in remote
environments each write is a container round-trip).  Later compaction then
shortens old observations without I/O: the reference already travels in the
message metadata and the model can page the original back with
``read_output``.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from agent_runtime.execution.base import OUTPUTS_DIR, Workspace
from agent_runtime.harness import (
    DEFAULT_HEAD_CHARS,
    DEFAULT_MAX_OBSERVATION_CHARS,
    DEFAULT_SPILL_PREVIEW_CHARS,
)

_UNSAFE_CHARS = re.compile(r"[^\w.-]")


@dataclass(frozen=True)
class RenderedObservation:
    """Transcript text for one tool result plus its archive reference.

    ``spill_path`` is set when the full text was written to ``.outputs/`` —
    the structured channel memory strategies read; the inline marker in
    ``text`` is the model-facing copy.
    """

    text: str
    spill_path: str | None = None


def _spill_name(call_id: str) -> str | None:
    """Deterministic archive filename for one tool call, or ``None`` when
    the call has no usable id."""
    safe = _UNSAFE_CHARS.sub("-", call_id).strip("-.")
    return f"obs-{safe}.txt" if safe else None


def format_tool_result(result: Any) -> str:
    """Render a tool result as compact observation text.

    Shell results get a dedicated minimal layout; other mappings render as
    JSON; everything else passes through ``str``.
    """
    if isinstance(result, str):
        return result
    if isinstance(result, Mapping):
        if "exit_code" in result and "stdout" in result:
            return _format_shell_result(result)
        try:
            return json.dumps(result, ensure_ascii=False, default=str)
        except (TypeError, ValueError):
            return str(result)
    return str(result)


def _format_shell_result(result: Mapping[str, Any]) -> str:
    """One compact block per shell result; empty fields never render."""
    lines: list[str] = []
    error = result.get("error")
    if isinstance(error, Mapping):
        lines.append(f"error: {error.get('type', 'Error')}: {error.get('message', '')}")
    exit_code = result.get("exit_code")
    if exit_code is not None and exit_code != 0:
        lines.append(f"exit_code: {exit_code}")
    stdout = result.get("stdout")
    if stdout:
        lines.append(stdout if isinstance(stdout, str) else str(stdout))
    stderr = result.get("stderr")
    if stderr:
        lines.append(f"--- stderr ---\n{stderr}")
    script_path = result.get("script_path")
    if script_path:
        lines.append(f"script: {script_path}")
    return "\n".join(lines) if lines else "ok (no output)"


class OutputSpill:
    """Archive observation text under ``.outputs/`` — the single spill writer.

    File names derive from the ``tool_call_id``, so re-archiving the same
    observation overwrites the same path instead of accumulating files.
    """

    def __init__(self, workspace: Workspace) -> None:
        self.workspace = workspace

    async def write(self, text: str, call_id: str) -> str | None:
        """Archive ``text`` for ``call_id``; returns the path, or ``None`` when
        the call has no usable id or the write fails (the caller must then
        degrade honestly instead of referencing a ghost file)."""
        name = _spill_name(call_id)
        if name is None:
            return None
        path = f"{OUTPUTS_DIR}/{name}"
        saved = await self.workspace.write_file(path, text)
        return None if "error" in saved else path


class ObservationFormatter:
    """Render tool results at the transcript boundary, archiving what could
    ever be lost.

    Compaction truncates observation text beyond ``head_chars``, so anything at
    or below that size skips the archive; ``archive_min_chars`` is derived from
    ``context.head_chars`` so the invariant is structural.
    """

    def __init__(self, workspace: Workspace,
                 max_chars: int | None = None,
                 spill_preview_chars: int | None = None,
                 archive_min_chars: int | None = None) -> None:
        self.max_chars = (max_chars if max_chars is not None
                          else DEFAULT_MAX_OBSERVATION_CHARS)
        self.spill_preview_chars = (spill_preview_chars if spill_preview_chars is not None
                                    else DEFAULT_SPILL_PREVIEW_CHARS)
        self.archive_min_chars = (archive_min_chars if archive_min_chars is not None
                                  else DEFAULT_HEAD_CHARS)
        self.spill = OutputSpill(workspace)

    async def render(self, result: Any, call_id: str = "") -> RenderedObservation:
        """Compact rendering of ``result``; archives it when the text is
        large enough that compaction could ever truncate it."""
        text = format_tool_result(result)
        spill_path = None
        if len(text) > self.archive_min_chars:
            spill_path = await self.spill.write(text, call_id)
        if len(text) <= self.max_chars:
            return RenderedObservation(text, spill_path)
        return RenderedObservation(self._window(text, spill_path), spill_path)

    def _window(self, text: str, spill_path: str | None) -> str:
        """Head/tail window with an honest marker.  When there is no real middle
        to omit (or no archive), the text passes through verbatim."""
        head, tail = (text[:self.spill_preview_chars],
                      text[-self.spill_preview_chars:])
        omitted = len(text) - len(head) - len(tail)
        if omitted <= 0:
            return text
        if spill_path:
            marker = (f"\n... [{omitted} characters omitted; full output saved to "
                      f"{spill_path}, page through it with read_output] ...\n")
        else:
            marker = f"\n... [{omitted} characters omitted] ...\n"
        return head + marker + tail


__all__ = ["OutputSpill", "ObservationFormatter", "RenderedObservation",
           "format_tool_result"]
