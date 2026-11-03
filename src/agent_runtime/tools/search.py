"""Search meta-tools: content search and filename matching.

Both tools walk the workspace once per call through the ``Workspace``
protocol (so they work locally and in remote environments), skip
well-known junk directories, and cap their output so a broad query can
never flood the context.  ``grep_search`` returns structured matches
whose ``line`` numbers line up with ``read_file``'s ``offset`` paging.
"""

from __future__ import annotations

import fnmatch
import logging
import posixpath
import re
from typing import Any

from agent_runtime.execution.base import (
    DEFAULT_FIND_RESULTS,
    DEFAULT_GREP_RESULTS,
    MATCHES_PER_FILE,
    MAX_FILE_BYTES,
    MAX_FIND_RESULTS,
    MAX_GREP_RESULTS,
    PREVIEW_CHARS,
    SKIP_DIRS,
    Workspace,
    matches_path,
)
from agent_runtime.execution.local import LocalWorkspace
from agent_runtime.tools.spec import ToolSpec
from functools import partial


logger = logging.getLogger(__name__)


async def walk_files(workspace: Workspace, path: str = ".",
                     ) -> dict[str, Any] | list[dict[str, Any]]:
    """Collect every file under ``path`` as ``{"path", "size"}`` entries.

    Returns a structured ``error`` result when the root listing itself
    fails; unreadable subdirectories are skipped silently.
    """
    listing = await workspace.list_dir(path)
    if "error" in listing:
        return listing
    files: list[dict[str, Any]] = []
    for entry in listing.get("entries", []):
        name = entry.get("name", "")
        child = name if path in (".", "") else posixpath.join(path, name)
        if entry.get("type") == "dir":
            if name in SKIP_DIRS:
                continue
            nested = await walk_files(workspace, child)
            if isinstance(nested, dict):  # unreadable subdirectory: skip
                continue
            files.extend(nested)
        elif entry.get("type") == "file":
            files.append({"path": child, "size": entry.get("size", 0)})
    return files


async def grep_search(pattern: str, path: str = ".",
                      include: str | None = None,
                      ignore_case: bool = False,
                      max_results: int = DEFAULT_GREP_RESULTS, *,
                      workspace: Workspace | None = None) -> dict[str, Any]:
    """Search file contents line by line with a regular expression.

    Matches come back as ``{"path", "line", "preview"}`` where ``line``
    is 1-based and feeds straight into ``read_file(offset=...)``.
    Output is bounded: at most ``max_results`` matches total and
    ``MATCHES_PER_FILE`` per file, with a ``truncated`` flag telling the
    model to narrow the pattern or set an ``include`` wildcard.
    """
    if not pattern:
        raise ValueError("pattern must not be empty")
    try:
        flags = re.IGNORECASE if ignore_case else 0
        regex = re.compile(pattern, flags)
    except re.error as exc:
        raise ValueError(f"invalid regex {pattern!r}: {exc}") from exc
    max_results = max(1, min(max_results, MAX_GREP_RESULTS))

    ws = workspace or LocalWorkspace()
    fast_search = getattr(ws, "search_contents", None)
    if callable(fast_search):
        try:
            outcome = await ws.search_contents(
                pattern, path, include, ignore_case, max_results)
        except ValueError:
            raise
        except Exception as exc:
            logger.warning("grep_search: fast search failed, falling back: %s",
                           exc)
        else:
            if outcome is not None:
                return outcome
    files = await walk_files(ws, path)
    if isinstance(files, dict):
        return {"pattern": pattern, "path": path, **files}

    matches: list[dict[str, Any]] = []
    truncated = False
    files_scanned = 0
    files_skipped = 0
    for file_entry in files:
        if truncated:
            break
        file_path = file_entry["path"]
        if include and not fnmatch.fnmatchcase(posixpath.basename(file_path),
                                               include):
            continue
        if file_entry.get("size", 0) > MAX_FILE_BYTES:
            files_skipped += 1
            continue
        files_scanned += 1
        read = await ws.read_file(file_path)
        if "error" in read:
            files_skipped += 1
            continue
        file_matches = 0
        for line_number, line in enumerate(read["content"].splitlines(), 1):
            if not regex.search(line):
                continue
            preview = line.strip()
            if len(preview) > PREVIEW_CHARS:
                preview = preview[:PREVIEW_CHARS - 1] + "…"
            matches.append({"path": file_path, "line": line_number,
                            "preview": preview})
            file_matches += 1
            if len(matches) >= max_results:
                truncated = True
                break
            if file_matches >= MATCHES_PER_FILE:
                break

    result: dict[str, Any] = {
        "pattern": pattern,
        "matches": matches,
        "match_count": len(matches),
        "truncated": truncated,
        "files_scanned": files_scanned,
        "files_skipped": files_skipped,
    }
    if truncated:
        result["note"] = ("results truncated; narrow the pattern, set "
                          "include (e.g. '*.py'), or raise max_results")
    return result


async def find_files(pattern: str, path: str = ".",
                     max_results: int = DEFAULT_FIND_RESULTS, *,
                     workspace: Workspace | None = None) -> dict[str, Any]:
    """Recursively find files whose workspace-relative paths match a wildcard pattern (e.g. ``**/*.py`` or ``userService*``). ``*`` may match across directory separators; patterns without ``/`` also match basenames anywhere under the search directory. Skips common generated and dependency directories."""
    if not pattern:
        raise ValueError("pattern must not be empty")
    max_results = max(1, min(max_results, MAX_FIND_RESULTS))

    ws = workspace or LocalWorkspace()
    fast_find = getattr(ws, "find_paths", None)
    if callable(fast_find):
        try:
            outcome = await ws.find_paths(pattern, path, max_results)
        except ValueError:
            raise
        except Exception as exc:
            logger.warning("find_files: fast search failed, falling back: %s",
                           exc)
        else:
            if outcome is not None:
                return outcome
    files = await walk_files(ws, path)
    if isinstance(files, dict):
        return {"pattern": pattern, "path": path, **files}

    matches = sorted(file_entry["path"] for file_entry in files
                     if matches_path(file_entry["path"], pattern))
    truncated = len(matches) > max_results
    result: dict[str, Any] = {
        "pattern": pattern,
        "matches": matches[:max_results],
        "match_count": min(len(matches), max_results),
        "total_matches": len(matches),
        "truncated": truncated,
    }
    if truncated:
        result["note"] = "results truncated; raise max_results for more"
    return result


def grep_search_spec(workspace: Workspace) -> ToolSpec:
    return ToolSpec("grep_search",
                    "Search file contents line by line with a regular "
                    "expression and return structured matches "
                    "(path, line, preview) whose line numbers feed "
                    "read_file(offset=...). Junk directories (.git, "
                    "node_modules, ...) are skipped automatically; output "
                    "is capped with a truncated flag.",
                    {"type": "object", "properties": {
                        "pattern": {"type": "string",
                                    "description": "Regular expression "
                                                   "(Python re syntax)"},
                        "path": {"type": "string",
                                 "description": "Directory inside the workspace "
                                                "to search",
                                 "default": "."},
                        "include": {"type": "string",
                                    "description": "Basename wildcard filter, "
                                                   "e.g. '*.py' (matched against "
                                                   "file names only)"},
                        "ignore_case": {"type": "boolean",
                                        "description": "Case-insensitive matching "
                                                       "when true",
                                        "default": False},
                        "max_results": {"type": "integer",
                                        "description": "Maximum matches to return "
                                                       "(capped server-side)",
                                        "default": DEFAULT_GREP_RESULTS}},
                     "required": ["pattern"]},
                    partial(grep_search, workspace=workspace))


def find_files_spec(workspace: Workspace) -> ToolSpec:
    return ToolSpec("find_files",
                    "Recursively find files whose workspace-relative paths "
                    "match a wildcard pattern (e.g. '**/*.py' or "
                    "'userService*'); '*' may match across directory "
                    "separators and patterns without '/' also match "
                    "basenames anywhere under the search directory. "
                    "Skips common generated and dependency directories. "
                    "Output is capped with a truncated flag. "
                    "One call replaces many list_dir round trips.",
                    {"type": "object", "properties": {
                        "pattern": {"type": "string",
                                    "description": "Wildcard pattern matched "
                                                   "against workspace-relative "
                                                   "file paths"},
                        "path": {"type": "string",
                                 "description": "Directory to search",
                                 "default": "."},
                        "max_results": {"type": "integer",
                                        "description": "Maximum paths",
                                        "default": DEFAULT_FIND_RESULTS}},
                     "required": ["pattern"]},
                    partial(find_files, workspace=workspace))


__all__ = [
    "DEFAULT_GREP_RESULTS", "DEFAULT_FIND_RESULTS", "MATCHES_PER_FILE",
    "MAX_FILE_BYTES", "MAX_GREP_RESULTS", "MAX_FIND_RESULTS", "PREVIEW_CHARS",
    "SKIP_DIRS", "find_files", "grep_search", "walk_files",
    "grep_search_spec", "find_files_spec",
]
