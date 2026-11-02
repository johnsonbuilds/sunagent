"""One short human-readable summary per tool call, shared by channels."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any


def describe_tool(tool: str | None, arguments: Any, *,
                  shell_prefix: bool = False) -> str:
    """Summarise a tool call in one line.

    ``shell_prefix`` lets the terminal renderer mark shell-like tools
    with ``$`` while the web UI keeps the same text without it.  Unknown
    tools fall back to their JSON arguments so no information is lost.
    """
    if not isinstance(arguments, Mapping):
        return "" if arguments is None else json.dumps(
            arguments, ensure_ascii=False, default=str)

    prefix = "$ " if shell_prefix else ""
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
            preview = first if len(first) <= 80 else first[:79] + "…"
            return f"$ {language} · {preview}"
    if tool == "write_file":
        content = arguments.get("content")
        size = len(content) if isinstance(content, str) else 0
        return f"write {arguments.get('path')} ({size} chars)"
    if tool == "grep_search":
        return f"{prefix}grep {arguments.get('pattern')}"
    if tool == "find_files":
        return f"{prefix}find {arguments.get('pattern')}"
    if tool == "find_symbol":
        return f"{prefix}symbol {arguments.get('name')}"
    if tool == "find_references":
        return f"{prefix}refs {arguments.get('name')}"
    if tool == "apply_patch":
        patch = arguments.get("patch")
        if isinstance(patch, str):
            return f"patch: {patch.count('>>>>>>> REPLACE')} edit(s)"
    if not arguments:
        return ""
    return json.dumps(dict(arguments), ensure_ascii=False, sort_keys=True)


__all__ = ["describe_tool"]
