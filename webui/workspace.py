"""Read-only workspace browsing for the ``/files`` page.

The page shows the same tree the agent's file tools see: the workspace
root is ``AGENT_RUNTIME_WORKSPACE`` when set, otherwise the process
working directory.  Everything here is read-only; no git index or file
is ever written.

Design notes:

* Every request re-reads the root from the environment, so changing
  ``AGENT_RUNTIME_WORKSPACE`` does not need a server restart.
* Paths crossing the API are POSIX paths relative to the root; ``""``
  means the root itself.  Symlinks are shown but never followed outside
  the root.
* ``.gitignore`` is the source of truth for "ignored"; a small built-in
  list covers non-git directories and the directories that would make
  the page unusable (``.git``, ``node_modules``, …).
* Git runs as a subprocess with ``--relative``, so a workspace that is a
  subdirectory of a repository still reports workspace-relative paths.
"""

from __future__ import annotations

import fnmatch
import os
import re
import stat
import subprocess
from pathlib import Path, PurePosixPath
from typing import Any, Iterable

# Directory entry and payload limits.  They keep one click from reading a
# multi-gigabyte tree into memory; the flags in the response tell the UI
# that it is looking at a prefix.
MAX_TREE_ENTRIES = 1000
MAX_TREE_SCAN = 5000
MAX_PREVIEW_BYTES = 2 * 1024 * 1024
PREVIEW_BYTES = 512 * 1024
MAX_PREVIEW_LINES = 5000
MAX_DIFF_BYTES = 2 * 1024 * 1024
MAX_DIFF_LINES = 4000
# A request may ask for any context in this range; "expand full file" uses
# the parser's line budget as its context, so the whole previewable file
# (up to 4000 lines) is included without generating an unbounded patch.
MAX_DIFF_CONTEXT = 2000
FULL_DIFF_CONTEXT = MAX_DIFF_LINES
MAX_CHANGES = 5000
MAX_UNTRACKED_BYTES = 2 * 1024 * 1024

GIT_STATUS_TIMEOUT = 5.0
GIT_DIFF_TIMEOUT = 10.0
GIT_TIMEOUT = 5.0

# Never listed, even when ignored files are shown: these are either huge,
# generated, or (in the case of .git) not something the page should read.
HARD_SKIP = {
    ".git",
    ".venv",
    "node_modules",
    "__pycache__",
    ".next",
}

# Used outside a git repository, and as a fallback when git is unavailable.
FALLBACK_IGNORE = {
    "workspace",
    "jobs",
    ".outputs",
    ".scripts",
    "out",
    "dist",
}

_PROTECTED_NAMES = {".env", ".netrc", "model.json"}
_PROTECTED_SUFFIXES = {".pem", ".key", ".p12"}
_PROTECTED_GLOBS = ("id_rsa*", "credentials*")
_ENV_PREFIX = ".env."

_HUNK_RE = re.compile(
    r"^@@ -(?P<old_start>\d+)(?:,(?P<old_count>\d+))? "
    r"\+(?P<new_start>\d+)(?:,(?P<new_count>\d+))? @@(?P<tail>.*)$"
)

_STATUS_PRIORITY = {
    "conflicted": 0,
    "untracked": 1,
    "renamed": 2,
    "added": 3,
    "deleted": 4,
    "modified": 5,
    "typechange": 6,
    "copied": 7,
}


class WorkspaceError(Exception):
    """Base class for browser failures the API turns into a response."""

    status = 400

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class InvalidPath(WorkspaceError):
    status = 400


class WorkspaceNotFound(WorkspaceError):
    status = 404


class ProtectedFile(WorkspaceError):
    status = 403


class GitError(WorkspaceError):
    status = 500


def _default_root() -> Path:
    raw = os.getenv("AGENT_RUNTIME_WORKSPACE")
    return Path(raw).expanduser().resolve() if raw else Path.cwd().resolve()


def is_protected_name(name: str) -> bool:
    """Whether a file name is on the never-read list."""
    lowered = name.lower()
    if name in _PROTECTED_NAMES or lowered in _PROTECTED_NAMES:
        return True
    if lowered.startswith(_ENV_PREFIX):
        return True
    if Path(lowered).suffix in _PROTECTED_SUFFIXES:
        return True
    return any(fnmatch.fnmatchcase(lowered, pattern)
               for pattern in _PROTECTED_GLOBS)


def _decode_text(raw: bytes, *, allow_incomplete_tail: bool = False) -> str | None:
    """Decode UTF-8, treating binary data as ``None``.

    A cut in the middle of a multi-byte character is our own doing when a
    large file is truncated; only that trailing case is salvaged.
    """
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        if allow_incomplete_tail and exc.start >= len(raw) - 3:
            return raw[:exc.start].decode("utf-8", errors="ignore")
        return None


def _has_nul(raw: bytes) -> bool:
    return b"\x00" in raw[:8192]


def _entry_type(path: Path) -> str:
    try:
        if path.is_symlink():
            return "symlink"
        if path.is_dir():
            return "dir"
        return "file"
    except OSError:
        return "file"


def _tail(value: bytes | str, limit: int = 500) -> str:
    if isinstance(value, bytes):
        text = value.decode("utf-8", errors="replace")
    else:
        text = value
    text = text.strip()
    return text[-limit:]


class WorkspaceBrowser:
    """Read-only view of one workspace root."""

    def __init__(self, root: str | Path | None = None) -> None:
        self._fixed_root = Path(root).expanduser().resolve() if root is not None else None
        self._repo_roots: dict[Path, Path | None] = {}

    @property
    def root(self) -> Path:
        return self._fixed_root if self._fixed_root is not None else _default_root()

    def repo_root(self) -> Path | None:
        """The enclosing git repository, or ``None`` outside one.

        A fixed root is cached; an environment-derived root is keyed by
        its current value, so a test that changes the variable between
        requests still sees the right answer.
        """
        root = self.root
        if root in self._repo_roots:
            return self._repo_roots[root]
        result: Path | None = None
        completed = self._run_git(root, ["rev-parse", "--show-toplevel"],
                                  timeout=GIT_TIMEOUT)
        if completed is not None and completed.returncode == 0:
            raw = completed.stdout.decode("utf-8", errors="replace").strip()
            if raw:
                candidate = Path(raw).resolve()
                if root.is_relative_to(candidate):
                    result = candidate
        self._repo_roots[root] = result
        return result

    def _has_head(self) -> bool:
        """Whether the repository has at least one commit."""
        completed = self._run_git(
            self.root, ["rev-parse", "--verify", "HEAD"], timeout=GIT_TIMEOUT)
        return completed is not None and completed.returncode == 0

    def _repo_context(self) -> tuple[Path | None, str]:
        """The enclosing repository and this workspace's prefix inside it.

        Git's status output is anchored at the repository root even when
        ``-C`` points at a subdirectory, so every git call here runs at
        the repository and paths are converted at the boundary.
        """
        repo = self.repo_root()
        if repo is None:
            return None, ""
        root = self.root
        prefix = "" if root == repo else root.relative_to(repo).as_posix()
        return repo, prefix

    @staticmethod
    def _repo_path(rel: str, prefix: str) -> str:
        return f"{prefix}/{rel}" if prefix else rel

    @staticmethod
    def _local_path(rel: str, prefix: str) -> str | None:
        if not prefix:
            return rel
        if rel == prefix:
            return ""
        marker = f"{prefix}/"
        if rel.startswith(marker):
            return rel[len(marker):]
        return None

    # -- paths -----------------------------------------------------------

    def _resolve(self, path: str | None, *, want: str = "any") -> Path:
        """Resolve an API path, refusing escapes, hard skips and symlinks.

        ``want`` is ``"dir"`` or ``"file"`` when the caller knows what it
        expects; a mismatched kind is an invalid request, not a 404.
        """
        rel = "" if path is None else path
        if "\x00" in rel:
            raise InvalidPath("path contains a NUL byte")
        if rel.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", rel):
            raise InvalidPath("path must be relative to the workspace root")
        pure = PurePosixPath(rel.replace("\\", "/"))
        if any(part == ".." for part in pure.parts):
            raise InvalidPath("path escapes the workspace")
        if any(part in HARD_SKIP for part in pure.parts):
            raise WorkspaceNotFound("path is not browsable")
        root = self.root
        target = (root / pure).resolve()
        if not target.is_relative_to(root):
            raise InvalidPath("path escapes the workspace")
        if target != root and (root / pure).is_symlink():
            raise InvalidPath("symbolic links are not followed")
        if target == root:
            if want == "file":
                raise InvalidPath("path is a directory")
            return target
        if not target.exists():
            raise WorkspaceNotFound(f"path does not exist: {pure.as_posix()}")
        if want == "dir" and not target.is_dir():
            raise InvalidPath("path is not a directory")
        if want == "file" and not target.is_file():
            raise InvalidPath("path is not a file")
        return target

    def _resolve_loose(self, path: str) -> Path:
        """Validate a path without requiring it to exist.

        Used for a rename's old path: git may report it after the file is
        gone, and diff still needs it as a pathspec.
        """
        rel = path
        if "\x00" in rel:
            raise InvalidPath("path contains a NUL byte")
        if rel.startswith("/") or re.match(r"^[A-Za-z]:[\\/]", rel):
            raise InvalidPath("path must be relative to the workspace root")
        pure = PurePosixPath(rel.replace("\\", "/"))
        if any(part == ".." for part in pure.parts):
            raise InvalidPath("path escapes the workspace")
        if any(part in HARD_SKIP for part in pure.parts):
            raise WorkspaceNotFound("path is not browsable")
        root = self.root
        target = (root / pure).resolve()
        if not target.is_relative_to(root):
            raise InvalidPath("path escapes the workspace")
        return target

    @staticmethod
    def _rel(target: Path, root: Path) -> str:
        return "" if target == root else target.relative_to(root).as_posix()

    # -- git -------------------------------------------------------------

    @staticmethod
    def _git_env() -> dict[str, str]:
        env = os.environ.copy()
        env.update({
            "GIT_OPTIONAL_LOCKS": "0",
            "GIT_TERMINAL_PROMPT": "0",
            "LC_ALL": "C",
        })
        return env

    def _run_git(self, cwd: Path | None, args: list[str], *,
                 timeout: float) -> subprocess.CompletedProcess[bytes] | None:
        """Run git without a shell; ``None`` means git is not installed."""
        try:
            return subprocess.run(
                ["git", "-C", str(cwd or self.root), "--no-pager", *args],
                capture_output=True,
                timeout=timeout,
                env=self._git_env(),
                check=False,
            )
        except FileNotFoundError:
            return None
        except subprocess.TimeoutExpired as exc:
            raise GitError(f"git timed out after {timeout:g}s") from exc
        except OSError as exc:
            raise GitError(f"could not run git: {exc}") from exc

    def _git(self, args: list[str], *, timeout: float = GIT_TIMEOUT,
             root: Path | None = None) -> subprocess.CompletedProcess[bytes]:
        completed = self._run_git(root or self.root, args, timeout=timeout)
        if completed is None:
            raise GitError("git is not installed")
        return completed

    def _git_text(self, args: list[str], *, timeout: float = GIT_TIMEOUT,
                  allow_codes: Iterable[int] = (0,),
                  root: Path | None = None) -> tuple[str, int]:
        completed = self._git(args, timeout=timeout, root=root)
        if completed.returncode not in set(allow_codes):
            raise GitError(
                f"git {' '.join(args[:2])} failed: {_tail(completed.stderr)}"
            )
        return completed.stdout.decode("utf-8", errors="replace"), completed.returncode

    # -- ignore ----------------------------------------------------------

    def _ignored(self, rel_paths: list[str]) -> set[str]:
        """Return the subset of workspace-relative paths git ignores."""
        if not rel_paths:
            return set()
        repo, prefix = self._repo_context()
        if repo is not None:
            repo_paths = [self._repo_path(rel, prefix) for rel in rel_paths]
            payload = "\0".join(repo_paths) + "\0"
            try:
                completed = subprocess.run(
                    ["git", "-C", str(repo), "--no-pager",
                     "check-ignore", "--stdin", "-z"],
                    input=payload.encode("utf-8"),
                    capture_output=True,
                    timeout=GIT_TIMEOUT,
                    env=self._git_env(),
                    check=False,
                )
            except (OSError, subprocess.TimeoutExpired):
                completed = None
            if completed is not None and completed.returncode in (0, 1):
                raw = completed.stdout.decode("utf-8", errors="replace")
                ignored = {
                    local
                    for item in raw.split("\0") if item
                    if (local := self._local_path(item, prefix)) is not None
                }
                return ignored | self._fallback_ignored(rel_paths)
        # Non-git workspace (or git unavailable): the built-in list is the
        # only signal.  It never hides ordinary source files.
        return self._fallback_ignored(rel_paths)

    @staticmethod
    def _fallback_ignored(rel_paths: list[str]) -> set[str]:
        """Runtime/output directories this deployment does not want listed.

        Older ``jobs/`` runs are tracked in this repository, so git does
        not report them as ignored; the page still treats them as such,
        and the "show ignored" switch reveals them.
        """
        return {
            rel for rel in rel_paths
            if any(part in FALLBACK_IGNORE for part in PurePosixPath(rel).parts)
        }

    # -- tree ------------------------------------------------------------

    def list_dir(self, path: str = "", include_ignored: bool = False) -> dict[str, Any]:
        root = self.root
        target = self._resolve(path, want="dir")
        rel_dir = self._rel(target, root)
        repo = self.repo_root()

        candidates: list[tuple[str, str, Path]] = []
        truncated = False
        scanned = 0
        try:
            with os.scandir(target) as entries:
                for entry in entries:
                    name = entry.name
                    if name in HARD_SKIP:
                        continue
                    scanned += 1
                    if scanned > MAX_TREE_SCAN:
                        truncated = True
                        break
                    rel = f"{rel_dir}/{name}" if rel_dir else name
                    candidates.append((name, rel, Path(entry.path)))
        except PermissionError as exc:
            raise InvalidPath(f"permission denied: {rel_dir or '.'}") from exc
        except OSError as exc:
            raise InvalidPath(str(exc)) from exc

        ignored = self._ignored([rel for _, rel, _ in candidates])

        entries = []
        hidden_ignored = 0
        for name, rel, child in candidates:
            is_ignored = rel in ignored
            if is_ignored and not include_ignored:
                hidden_ignored += 1
                continue
            kind = _entry_type(child)
            try:
                info = child.stat(follow_symlinks=False)
                size = None if kind == "dir" else info.st_size
                mtime = info.st_mtime
            except OSError:
                size, mtime = None, 0.0
            entries.append({
                "name": name,
                "path": rel,
                "type": kind,
                "size": size,
                "mtime": mtime,
                "ignored": is_ignored,
                "protected": kind != "dir" and is_protected_name(name),
            })

        entries.sort(key=lambda item: (item["type"] != "dir",
                                       item["name"].lower(),
                                       item["name"]))
        if len(entries) > MAX_TREE_ENTRIES:
            entries = entries[:MAX_TREE_ENTRIES]
            truncated = True
        return {
            "root": str(root),
            "repo_root": str(repo) if repo is not None else None,
            "is_git": repo is not None,
            "path": rel_dir,
            "entries": entries,
            "hidden_ignored": hidden_ignored,
            "truncated": truncated,
        }

    # -- file preview ----------------------------------------------------

    def read_file(self, path: str) -> dict[str, Any]:
        root = self.root
        target = self._resolve(path, want="file")
        rel = self._rel(target, root)
        if is_protected_name(target.name):
            raise ProtectedFile("this file is not readable from the web UI")

        try:
            info = target.stat()
            size, mtime = info.st_size, info.st_mtime
            with target.open("rb") as handle:
                raw = handle.read(MAX_PREVIEW_BYTES)
        except OSError as exc:
            raise WorkspaceNotFound(f"could not read {rel}: {exc}") from exc

        truncated = size > MAX_PREVIEW_BYTES
        if truncated:
            raw = raw[:PREVIEW_BYTES]
        text = None if _has_nul(raw) else _decode_text(
            raw, allow_incomplete_tail=truncated)
        if text is None:
            return {
                "path": rel,
                "size": size,
                "mtime": mtime,
                "binary": True,
                "truncated": truncated,
                "lines": None,
                "encoding": None,
                "content": None,
            }

        # ``keepends`` preserves CRLF and a missing final newline exactly;
        # the editor round-trip is not part of this page, but the preview
        # should still show what is on disk.
        lines = text.splitlines(keepends=True)
        if len(lines) > MAX_PREVIEW_LINES:
            lines = lines[:MAX_PREVIEW_LINES]
            truncated = True
        return {
            "path": rel,
            "size": size,
            "mtime": mtime,
            "binary": False,
            "truncated": truncated,
            "lines": len(lines),
            "encoding": "utf-8",
            "content": "".join(lines),
        }

    # -- changes ---------------------------------------------------------

    def changes(self) -> dict[str, Any]:
        root = self.root
        repo = self.repo_root()
        empty = {
            "root": str(root),
            "repo_root": None,
            "is_git": False,
            "git_error": None,
            "branch": None,
            "head": None,
            "upstream": None,
            "ahead": 0,
            "behind": 0,
            "entries": [],
            "counts": {
                "total": 0,
                "modified": 0,
                "added": 0,
                "deleted": 0,
                "renamed": 0,
                "untracked": 0,
                "conflicted": 0,
            },
            "truncated": False,
        }
        if repo is None:
            return empty

        # ``git status`` anchors paths at the repository root, so the call
        # runs there and every path is converted to workspace-relative.
        raw, _ = self._git_text(
            ["status", "--porcelain=v2", "--branch", "-z",
             "--untracked-files=all"],
            timeout=GIT_STATUS_TIMEOUT,
            root=repo,
        )
        branch, head, upstream, ahead, behind, parsed = _parse_status(raw)
        prefix = "" if root == repo else root.relative_to(repo).as_posix()
        entries: list[dict[str, Any]] = []
        for entry in parsed:
            local = self._local_path(entry["path"], prefix)
            if local is None:
                continue
            entry["path"] = local
            if entry["old_path"]:
                entry["old_path"] = self._local_path(entry["old_path"], prefix)
            entries.append(entry)

        has_head = self._has_head()
        stats = self._numstat(repo) if has_head else {}
        for entry in entries:
            path = entry["path"]
            if entry["status"] == "untracked" or not has_head:
                additions, deletions, binary = self._count_untracked(path)
            else:
                repo_rel = self._repo_path(path, prefix)
                additions, deletions, binary = stats.get(
                    repo_rel, (None, None, False))
            entry["additions"] = additions
            entry["deletions"] = deletions
            entry["binary"] = binary
            entry["protected"] = is_protected_name(Path(path).name)

        entries.sort(key=lambda item: item["path"])
        counts = {key: 0 for key in empty["counts"]}
        counts["total"] = len(entries)
        for entry in entries:
            if entry["status"] in counts:
                counts[entry["status"]] += 1

        truncated = len(entries) > MAX_CHANGES
        if truncated:
            entries = entries[:MAX_CHANGES]

        return {
            **empty,
            "repo_root": str(repo),
            "is_git": True,
            "branch": branch,
            "head": head,
            "upstream": upstream,
            "ahead": ahead,
            "behind": behind,
            "entries": entries,
            "counts": counts,
            "truncated": truncated,
        }

    def _numstat(self, repo: Path) -> dict[str, tuple[int | None, int | None, bool]]:
        """Map repository-relative path -> additions/deletions/binary.

        ``-M20%`` pairs a rename even after the renamed file was edited;
        the default 50% threshold calls that pair a delete plus an add and
        loses the line counts.
        """
        raw, _ = self._git_text(
            ["diff", "HEAD", "--numstat", "-z", "-M20%"],
            timeout=GIT_DIFF_TIMEOUT,
            root=repo,
        )
        result: dict[str, tuple[int | None, int | None, bool]] = {}
        tokens = raw.split("\0")
        index = 0
        while index < len(tokens):
            token = tokens[index]
            index += 1
            if not token:
                continue
            parts = token.split("\t")
            if len(parts) < 3:
                continue
            added, removed, path = parts[0], parts[1], parts[2]
            if path == "" and index + 1 < len(tokens):
                # Rename form: "add\tdel\t\0old\0new\0".
                old = tokens[index]
                new = tokens[index + 1] if index + 1 < len(tokens) else ""
                index += 2
                path = new or old
            binary = added == "-" or removed == "-"
            try:
                additions = None if binary else int(added)
                deletions = None if binary else int(removed)
            except ValueError:
                additions, deletions, binary = None, None, True
            if path:
                result[path] = (additions, deletions, binary)
        return result

    def _count_untracked(self, path: str) -> tuple[int | None, int | None, bool]:
        target = (self.root / PurePosixPath(path)).resolve()
        if not target.is_relative_to(self.root) or not target.is_file():
            return None, None, False
        try:
            with target.open("rb") as handle:
                raw = handle.read(MAX_UNTRACKED_BYTES)
        except OSError:
            return None, None, False
        text = _decode_text(raw, allow_incomplete_tail=len(raw) >= MAX_UNTRACKED_BYTES)
        if _has_nul(raw) or text is None:
            return None, None, True
        return len(text.splitlines()), 0, False

    # -- diff ------------------------------------------------------------

    def diff(self, path: str, old_path: str | None = None,
             context: int = 3, full: bool = False) -> dict[str, Any]:
        if not isinstance(context, int) or isinstance(context, bool) or \
                context < 0 or context > MAX_DIFF_CONTEXT:
            raise InvalidPath(
                f"context must be between 0 and {MAX_DIFF_CONTEXT}")
        effective_context = FULL_DIFF_CONTEXT if full else context
        root = self.root
        try:
            target = self._resolve(path, want="file")
        except WorkspaceNotFound:
            # A deleted file is still diffable; only the pathspec is needed.
            target = self._resolve_loose(path)
        rel = self._rel(target, root)
        if is_protected_name(target.name):
            raise ProtectedFile("this file is not readable from the web UI")
        repo, prefix = self._repo_context()
        if repo is None:
            raise InvalidPath("the workspace is not a git repository")

        old_rel: str | None = None
        if old_path:
            old_target = self._resolve_loose(old_path)
            if is_protected_name(old_target.name):
                raise ProtectedFile(
                    "this file is not readable from the web UI")
            old_rel = self._rel(old_target, root)

        repo_rel = self._repo_path(rel, prefix)
        tracked = self._is_tracked(repo_rel, repo)
        if not tracked or not self._has_head():
            if not target.is_file():
                raise WorkspaceNotFound(f"path does not exist: {rel}")
            return self._untracked_diff(rel, target, effective_context)

        args = ["diff", "HEAD", "--no-color", "--no-ext-diff", "-M20%",
                f"-U{effective_context}", "--", repo_rel]
        if old_rel:
            args.append(self._repo_path(old_rel, prefix))
        completed = self._git(args, timeout=GIT_DIFF_TIMEOUT, root=repo)
        if completed.returncode not in (0, 1):
            raise GitError(f"git diff failed: {_tail(completed.stderr)}")
        raw = completed.stdout
        if not raw:
            return _diff_payload(rel, old_rel, "modified", False, False, [],
                                 effective_context)

        truncated = len(raw) > MAX_DIFF_BYTES
        if truncated:
            raw = raw[:MAX_DIFF_BYTES]
        text = raw.decode("utf-8", errors="replace")
        if _patch_is_binary(text):
            return _diff_payload(rel, old_rel, _status_from_patch(text),
                                 True, truncated, [], effective_context)
        hunks, binary, truncated = _parse_diff(
            text, truncated=truncated, max_lines=MAX_DIFF_LINES)
        if binary:
            return _diff_payload(rel, old_rel, _status_from_patch(text),
                                 True, truncated, [], effective_context)
        status = _status_from_patch(text)
        return _diff_payload(rel, old_rel, status, False, truncated, hunks,
                             effective_context)

    def _is_tracked(self, repo_rel: str, repo: Path) -> bool:
        completed = self._git(
            ["ls-files", "--error-unmatch", "--", repo_rel],
            timeout=GIT_TIMEOUT,
            root=repo,
        )
        return completed.returncode == 0

    def _untracked_diff(self, rel: str, target: Path,
                        context: int = 3) -> dict[str, Any]:
        try:
            size = target.stat().st_size
            with target.open("rb") as handle:
                raw = handle.read(MAX_UNTRACKED_BYTES)
        except OSError as exc:
            raise WorkspaceNotFound(f"could not read {rel}: {exc}") from exc
        truncated = size > MAX_UNTRACKED_BYTES
        text = _decode_text(raw, allow_incomplete_tail=truncated)
        if _has_nul(raw) or text is None:
            return _diff_payload(rel, None, "added", True, truncated, [],
                                 context)

        lines = text.split("\n")
        if lines and lines[-1] == "":
            lines.pop()
        if len(lines) > MAX_DIFF_LINES:
            lines = lines[:MAX_DIFF_LINES]
            truncated = True
        rendered = [
            {"kind": "add", "old": None, "new": number, "text": line}
            for number, line in enumerate(lines, start=1)
        ]
        if text and not text.endswith("\n"):
            rendered.append({
                "kind": "note", "old": None, "new": None,
                "text": "\\ No newline at end of file",
            })
        hunk = {
            "header": f"@@ -0,0 +1,{len(lines)} @@",
            "old_start": 0,
            "old_count": 0,
            "new_start": 1,
            "new_count": len(lines),
            "lines": rendered,
        }
        return _diff_payload(rel, None, "added", False, truncated,
                             [hunk] if lines else [], context)


# -- parsing helpers -----------------------------------------------------


def _parse_status(raw: str) -> tuple[
        str | None, str | None, str | None, int, int, list[dict[str, Any]]]:
    """Parse ``git status --porcelain=v2 --branch -z`` output."""
    branch: str | None = None
    head: str | None = None
    upstream: str | None = None
    ahead = behind = 0
    entries: list[dict[str, Any]] = []

    tokens = raw.split("\0")
    index = 0
    while index < len(tokens):
        token = tokens[index]
        index += 1
        if not token:
            continue
        if token.startswith("# "):
            if token.startswith("# branch.head "):
                branch = token[len("# branch.head "):]
            elif token.startswith("# branch.oid "):
                head = token[len("# branch.oid "):]
            elif token.startswith("# branch.upstream "):
                upstream = token[len("# branch.upstream "):]
            elif token.startswith("# branch.ab "):
                match = re.match(r"# branch\.ab \+(\d+) -(\d+)$", token)
                if match:
                    ahead, behind = int(match.group(1)), int(match.group(2))
            continue
        if token.startswith("1 "):
            parts = token.split(" ", 8)
            if len(parts) < 9:
                continue
            xy = parts[1]
            path = parts[8]
            entries.append(_change_entry(path, xy, None))
        elif token.startswith("2 "):
            parts = token.split(" ", 9)
            if len(parts) < 10:
                continue
            xy = parts[1]
            path = parts[9]
            old_path = tokens[index] if index < len(tokens) else None
            index += 1
            entries.append(_change_entry(path, xy, old_path))
        elif token.startswith("u "):
            parts = token.split(" ", 10)
            if len(parts) < 11:
                continue
            entries.append(_change_entry(parts[10], parts[1], None))
        elif token.startswith("? "):
            entries.append({
                "path": token[2:],
                "old_path": None,
                "status": "untracked",
                "staged": False,
                "unstaged": True,
            })
        # "!" (ignored) is only printed with --ignored; ignore it here.

    if branch == "(detached)" or branch is None:
        branch = head[:7] if head and head != "(initial)" else branch
    if head == "(initial)":
        head = None
    elif head:
        head = head[:7]
    return branch, head, upstream, ahead, behind, entries


def _change_entry(path: str, xy: str, old_path: str | None) -> dict[str, Any]:
    index_status, worktree_status = (xy + "..")[:2]
    staged = index_status not in (".", " ")
    unstaged = worktree_status not in (".", " ")

    flags: set[str] = set()
    for char in (index_status, worktree_status):
        if char in ("U", "u"):
            flags.add("conflicted")
        elif char == "R":
            flags.add("renamed")
        elif char == "C":
            flags.add("copied")
        elif char == "A":
            flags.add("added")
        elif char == "D":
            flags.add("deleted")
        elif char == "T":
            flags.add("typechange")
    if not flags:
        flags.add("modified")
    status = min(flags, key=lambda item: _STATUS_PRIORITY.get(item, 99))
    return {
        "path": path,
        "old_path": old_path,
        "status": status,
        "staged": staged,
        "unstaged": unstaged,
    }


def _patch_is_binary(text: str) -> bool:
    """Whether a patch carries git's binary marker.

    Only header lines count: a changed source file may itself contain
    the words "Binary files" or "GIT binary patch".
    """
    for line in text.split("\n"):
        if line == "GIT binary patch":
            return True
        if line.startswith("Binary files ") and line.endswith(" differ"):
            return True
    return False


def _status_from_patch(text: str) -> str:
    """Read status from header lines, never from added file content."""
    for line in text.split("\n"):
        if line.startswith("new file mode "):
            return "added"
        if line.startswith("deleted file mode "):
            return "deleted"
        if line.startswith("rename from ") or line.startswith("rename to "):
            return "renamed"
        if line.startswith("old mode ") or line.startswith("new mode "):
            return "typechange"
    return "modified"


def _parse_diff(text: str, *, truncated: bool = False,
                max_lines: int = MAX_DIFF_LINES
                ) -> tuple[list[dict[str, Any]], bool, bool]:
    """Turn a unified diff into hunk dictionaries.

    Returns ``(hunks, binary, truncated)``.  Lines outside hunks (file
    headers, mode lines, combined-diff markers) are dropped; unknown
    lines inside a hunk become ``note`` lines so a conflict diff still
    renders something useful.
    """
    hunks: list[dict[str, Any]] = []
    current: dict[str, Any] | None = None
    old_no = new_no = 0
    line_count = 0
    binary = False

    raw_lines = text.split("\n")
    if raw_lines and raw_lines[-1] == "":
        raw_lines.pop()
    for line in raw_lines:
        if line.startswith("Binary files ") and line.endswith(" differ"):
            binary = True
            continue
        match = _HUNK_RE.match(line)
        if match:
            current = {
                "header": line,
                "old_start": int(match.group("old_start")),
                "old_count": int(match.group("old_count") or 1),
                "new_start": int(match.group("new_start")),
                "new_count": int(match.group("new_count") or 1),
                "lines": [],
            }
            old_no = current["old_start"]
            new_no = current["new_start"]
            hunks.append(current)
            continue
        if current is None:
            continue
        if line_count >= max_lines:
            truncated = True
            break
        line_count += 1

        if line.startswith("+"):
            current["lines"].append(
                {"kind": "add", "old": None, "new": new_no, "text": line[1:]})
            new_no += 1
        elif line.startswith("-"):
            current["lines"].append(
                {"kind": "del", "old": old_no, "new": None, "text": line[1:]})
            old_no += 1
        elif line.startswith(" "):
            current["lines"].append({
                "kind": "ctx", "old": old_no, "new": new_no, "text": line[1:]})
            old_no += 1
            new_no += 1
        elif line.startswith("\\"):
            current["lines"].append(
                {"kind": "note", "old": None, "new": None, "text": line})
        else:
            current["lines"].append(
                {"kind": "note", "old": None, "new": None, "text": line})

    return hunks, binary, truncated


def _diff_payload(path: str, old_path: str | None, status: str,
                  binary: bool, truncated: bool,
                  hunks: list[dict[str, Any]],
                  context: int = 3) -> dict[str, Any]:
    return {
        "path": path,
        "old_path": old_path,
        "status": status,
        "binary": binary,
        "truncated": truncated,
        "context": context,
        "hunks": hunks,
    }


__all__ = [
    "GitError",
    "InvalidPath",
    "ProtectedFile",
    "WorkspaceBrowser",
    "WorkspaceError",
    "WorkspaceNotFound",
]
