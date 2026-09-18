"""Harbor environment-backed shell execution and file workspace."""

from __future__ import annotations

import base64
import binascii
import math
import posixpath
import shlex
import time
from collections.abc import Awaitable
from typing import Any, Protocol

from .base import paginate_lines


class HarborEnvironment(Protocol):
    def exec(
        self,
        command: str,
        cwd: str | None = None,
        timeout_sec: int | None = None,
    ) -> Awaitable[Any]:
        """Execute a command in the Harbor environment."""


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return str(value)


class HarborShellExecutor:
    """Adapt a Harbor environment's async ``exec`` method to ShellExecutor."""

    def __init__(self, environment: HarborEnvironment) -> None:
        self.environment = environment

    async def execute(
        self,
        command: str,
        cwd: str | None = None,
        timeout: float | None = 30.0,
    ) -> dict[str, Any]:
        started = time.monotonic()
        try:
            timeout_sec = None if timeout is None else max(1, math.ceil(timeout))
            result = await self.environment.exec(
                command,
                cwd=cwd,
                timeout_sec=timeout_sec,
            )
        except Exception as exc:
            return {
                "stdout": "",
                "stderr": "",
                "exit_code": None,
                "duration": time.monotonic() - started,
                "error": {"type": type(exc).__name__, "message": str(exc)},
            }

        return {
            "stdout": _text(result.stdout),
            "stderr": _text(result.stderr),
            "exit_code": result.return_code,
            "duration": time.monotonic() - started,
        }


__all__ = ["HarborEnvironment", "HarborShellExecutor", "HarborWorkspace"]


class HarborWorkspace:
    """Workspace backed by a Harbor environment's ``exec``.

    File content crosses the boundary base64-encoded, so no quoting or
    escaping can corrupt it.  The container itself is the sandbox: with
    no root configured, paths are used as-is (relative paths resolve
    against the environment's default working directory).  With a root
    configured, paths are normalized and must stay inside it; symlinks
    cannot be resolved remotely, so containment is purely lexical.

    Listing relies on GNU find's ``-printf`` (present in the Ubuntu
    images used by terminal-bench).
    """

    def __init__(self, environment: HarborEnvironment,
                 root: str | None = None) -> None:
        self.environment = environment
        self.root = root

    def _resolve(self, path: str) -> str:
        if self.root is None:
            return posixpath.normpath(path)
        candidate = path if path.startswith("/") else posixpath.join(self.root, path)
        resolved = posixpath.normpath(candidate)
        root = posixpath.normpath(self.root)
        if resolved != root and not resolved.startswith(root + "/"):
            raise ValueError(f"path escapes the workspace: {path}")
        return resolved

    async def _exec(self, command: str) -> dict[str, Any]:
        """Run a helper command; a non-zero exit becomes a structured error."""
        try:
            result = await self.environment.exec(command, cwd=None, timeout_sec=60)
        except Exception as exc:
            return {"error": {"type": type(exc).__name__, "message": str(exc)}}
        code = result.return_code
        if code != 0:
            message = _text(result.stderr).strip() or f"exit code {code}"
            return {"error": {"type": "CommandError", "message": message}}
        return {"stdout": _text(result.stdout), "stderr": _text(result.stderr)}

    async def read_file(self, path: str, offset: int = 1,
                        limit: int | None = None) -> dict[str, Any]:
        target = self._resolve(path)
        outcome = await self._exec(f"base64 < {shlex.quote(target)}")
        if "error" in outcome:
            return {"path": path, "error": outcome["error"]}
        try:
            text = base64.b64decode(outcome["stdout"]).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError) as exc:
            return {"path": path,
                    "error": {"type": type(exc).__name__, "message": str(exc)}}
        return {"path": path, **paginate_lines(text, offset, limit)}

    async def write_file(self, path: str, content: str) -> dict[str, Any]:
        target = self._resolve(path)
        encoded = base64.b64encode(content.encode("utf-8")).decode("ascii")
        parent = posixpath.dirname(target)
        command = (f"mkdir -p {shlex.quote(parent)} && "
                   f"printf %s {shlex.quote(encoded)} | base64 -d > {shlex.quote(target)}")
        outcome = await self._exec(command)
        if "error" in outcome:
            return {"path": path, "error": outcome["error"]}
        return {"path": path, "bytes_written": len(content.encode("utf-8"))}

    async def list_dir(self, path: str = ".") -> dict[str, Any]:
        target = self._resolve(path)
        message = shlex.quote(f"not a directory: {target}")
        command = (f"if test -d {shlex.quote(target)}; then "
                   f"find {shlex.quote(target)} -mindepth 1 -maxdepth 1 "
                   f"-printf '%y\\t%s\\t%f\\n'; "
                   f"else echo {message} >&2; exit 1; fi")
        outcome = await self._exec(command)
        if "error" in outcome:
            return {"path": path, "error": outcome["error"]}
        entries: list[dict[str, Any]] = []
        for line in outcome["stdout"].splitlines():
            parts = line.split("\t", 2)
            if len(parts) != 3 or not parts[1].isdigit():
                continue
            kind, raw_size, name = parts
            entries.append({"name": name,
                            "type": {"f": "file", "d": "dir"}.get(kind, "other"),
                            "size": int(raw_size)})
        entries.sort(key=lambda entry: (entry["type"] != "dir", entry["name"].lower()))
        return {"path": path, "entries": entries}

    async def search_contents(self, pattern: str, path: str = ".",
                              include: str | None = None,
                              ignore_case: bool = False,
                              max_results: int = 200) -> dict[str, Any] | None:
        """Single-exec server-side grep via rg (fallback grep -rn).

        Same contract as tools.grep_search. Returns None only when the
        container has neither rg nor grep (caller falls back to walk+read).
        borrows labs-OO-Agents fail-closed ideas: rg exit 2 (bad regex
        dialect) is reported as an error instead of guessed anchors.
        """
        from agent_runtime.tools.search import (MATCHES_PER_FILE,
                                                MAX_FILE_BYTES, PREVIEW_CHARS,
                                                SKIP_DIRS)
        import fnmatch

        if not pattern:
            raise ValueError("pattern must not be empty")
        max_results = max(1, min(max_results, 1000))
        target = self._resolve(path)

        def build_rg(extra_args: list[str]) -> str:
            parts: list[str] = ["rg", "--vimgrep", "--no-heading",
                                "--hidden", "--no-ignore",
                                "--max-filesize", str(MAX_FILE_BYTES),
                                "--max-count", str(MATCHES_PER_FILE)]
            if ignore_case:
                parts.append("-i")
            for junk in sorted(SKIP_DIRS):
                parts += ["--glob", f"!{junk}/**", "--glob", f"!{junk}"]
            if include:
                parts += ["--glob", include]
            parts += extra_args + ["--", pattern, target]
            return " ".join(shlex.quote(p) for p in parts)

        async def run_raw(command: str) -> Any:
            try:
                return await self.environment.exec(command, cwd=None,
                                                   timeout_sec=60)
            except Exception as exc:
                return exc

        result = await run_raw(build_rg([]))
        if isinstance(result, Exception):
            return {"error": {"type": type(result).__name__,
                              "message": str(result)}}
        if result.return_code not in (0, 1):
            retry = await run_raw(build_rg(["--pcre2"]))
            if isinstance(retry, Exception):
                return {"error": {"type": type(retry).__name__,
                                  "message": str(retry)}}
            if retry.return_code not in (0, 1):
                msg = _text(retry.stderr).strip() or f"exit code {retry.return_code}"
                if retry.return_code == 127:
                    return None
                return {"pattern": pattern, "path": path,
                        "error": {"type": "SearchError", "message": msg}}
            result = retry
        if result.return_code == 1:
            return {"pattern": pattern, "matches": [], "match_count": 0,
                    "truncated": False, "files_scanned": 0, "files_skipped": 0}

        matches: list[dict[str, Any]] = []
        truncated = False
        seen_files: set[str] = set()
        per_file: dict[str, int] = {}
        root = posixpath.normpath(self.root) if self.root is not None else None
        for line in _text(result.stdout).splitlines():
            parts = line.split(":", 3)
            if len(parts) != 4:
                continue
            raw_path, raw_line, _col, text = parts
            if not raw_line.isdigit():
                continue
            rel = self._to_workspace_path(raw_path, path, target, root)
            if include and not fnmatch.fnmatchcase(posixpath.basename(rel),
                                                   include):
                continue
            seen_files.add(rel)
            count = per_file.get(rel, 0)
            if count >= MATCHES_PER_FILE:
                continue
            if len(matches) >= max_results:
                truncated = True
                break
            preview = text.strip()
            if len(preview) > PREVIEW_CHARS:
                preview = preview[:PREVIEW_CHARS - 1] + "…"
            matches.append({"path": rel, "line": int(raw_line),
                            "preview": preview})
            per_file[rel] = count + 1
        out: dict[str, Any] = {
            "pattern": pattern,
            "matches": matches,
            "match_count": len(matches),
            "truncated": truncated,
            "files_scanned": len(seen_files),
            "files_skipped": 0,
        }
        if truncated:
            out["note"] = ("results truncated; narrow the pattern, set "
                           "include (e.g. '*.py'), or raise max_results")
        return out

    def _to_workspace_path(self, raw_path: str, search_path: str,
                           target: str, root: str | None) -> str:
        """Map a container-side hit back to a workspace-relative path."""
        cleaned = raw_path[2:] if raw_path.startswith("./") else raw_path
        if root is not None:
            norm = posixpath.normpath(cleaned)
            if norm == root:
                return search_path
            if norm.startswith(root + "/"):
                return norm[len(root) + 1:]
            return cleaned
        norm_target = posixpath.normpath(target)
        if norm_target in (".", ""):
            return cleaned
        norm_clean = posixpath.normpath(cleaned)
        if norm_clean == norm_target:
            return search_path
        if norm_clean.startswith(norm_target + "/"):
            return norm_clean
        return cleaned

    async def find_paths(self, pattern: str, path: str = ".",
                         max_results: int = 100) -> dict[str, Any] | None:
        """Single-exec server-side filename listing via find."""
        from agent_runtime.tools.search import SKIP_DIRS, _matches_path

        if not pattern:
            raise ValueError("pattern must not be empty")
        max_results = max(1, min(max_results, 1000))
        target = self._resolve(path)
        prune = "".join(
            f" -path {shlex.quote(f'*/{junk}')} -prune -o"
            for junk in sorted(SKIP_DIRS)
        )
        command = (f"find {shlex.quote(target)}{prune} -type f -print")
        try:
            result = await self.environment.exec(command, cwd=None,
                                                 timeout_sec=60)
        except Exception as exc:
            return {"error": {"type": type(exc).__name__, "message": str(exc)}}
        if result.return_code != 0:
            if result.return_code == 127:
                return None
            msg = _text(result.stderr).strip() or f"exit code {result.return_code}"
            return {"pattern": pattern, "path": path,
                    "error": {"type": "CommandError", "message": msg}}
        root = posixpath.normpath(self.root) if self.root is not None else None
        rel_paths: list[str] = []
        for line in _text(result.stdout).splitlines():
            line = line.strip()
            if not line:
                continue
            rel_paths.append(self._to_workspace_path(line, path, target, root))
        matches = sorted(p for p in rel_paths if _matches_path(p, pattern))
        truncated = len(matches) > max_results
        return {
            "pattern": pattern,
            "matches": matches[:max_results],
            "match_count": min(len(matches), max_results),
            "total_matches": len(matches),
            "truncated": truncated,
            **({"note": "results truncated; raise max_results for more"}
               if truncated else {}),
        }
