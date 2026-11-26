"""Best-effort dangerous-command guard for local shell execution.

This is a fool-proofing layer, not a security boundary.  It catches
destructive or sensitive commands a model issues by mistake (for
example ``rm -rf /`` while cleaning a workspace).  A determined or
obfuscated command can bypass textual matching, and a
Turing-complete ``execute_code`` script can read files directly; the
real sandbox is OS-level isolation (the Harbor container path).

Policies:

* ``off``        — no interception (tests, trusted automation).
* ``blocklist``  — block destructive commands, sensitive targets,
                   privilege escalation, and pipe-to-shell (default).
* ``allowlist``  — additionally require every command to start with a
                   known-safe prefix.  High-lockdown environments only;
                   breaks most real tasks, including terminal-bench.

Matching normalizes whitespace and quotes, and resolves ``..``
segments in paths, so ``/etc/../etc/shadow`` is caught like
``/etc/shadow``.  A blocked command never raises: it comes back as a
structured ``{"error": {"type": "CommandBlocked", "message": ...}}``
result so the agent loop can turn it into an observation and try a
safer route.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any


DEFAULT_ALLOWLIST = (
    "ls", "cat", "head", "tail", "grep", "find", "pwd", "echo", "printf",
    "wc", "sort", "uniq", "cut", "awk", "sed", "python3", "python", "pip",
    "git", "mkdir", "touch", "cp", "mv", "diff", "which", "env", "date",
    "true", "false", "test",
)

BLOCK_REASON_DESTRUCTIVE = "destructive command"
BLOCK_REASON_SENSITIVE = "sensitive target"
BLOCK_REASON_PRIVILEGE = "privilege escalation"
BLOCK_REASON_REMOTE_PIPE = "remote content piped into a shell"
BLOCK_REASON_NOT_ALLOWLISTED = "command not in allowlist"


@dataclass(frozen=True)
class GuardDecision:
    """What the guard decided about one command."""

    allowed: bool
    reason: str | None = None


class CommandGuard:
    """Decide whether a command may run locally; textual, best-effort."""

    def __init__(self, policy: str = "blocklist",
                 workspace_root: str | None = None,
                 allowlist: tuple[str, ...] = DEFAULT_ALLOWLIST) -> None:
        if policy not in {"off", "blocklist", "allowlist"}:
            raise ValueError(f"unknown guard policy: {policy!r}")
        self.policy = policy
        self.workspace_root = workspace_root
        self.allowlist = allowlist

    def check(self, command: str) -> GuardDecision:
        """Evaluate one command line against the current policy."""
        if self.policy == "off":
            return GuardDecision(True)
        normalized = _normalize(command)
        for verdict in (self._check_destructive(normalized),
                        self._check_sensitive(normalized),
                        self._check_privilege(normalized),
                        self._check_remote_pipe(normalized)):
            if verdict is not None:
                return verdict
        if self.policy == "allowlist":
            return self._check_allowlist(normalized)
        return GuardDecision(True)

    def _check_destructive(self, command: str) -> GuardDecision | None:
        if _FORK_BOMB.search(command):
            return GuardDecision(False, f"{BLOCK_REASON_DESTRUCTIVE}: fork bomb")
        if _RMRecursive.search(command):
            target = _RMRecursive.search(command).group("target")
            if _recursive_rm_is_dangerous(target, self.workspace_root):
                return GuardDecision(
                    False,
                    f"{BLOCK_REASON_DESTRUCTIVE}: recursive rm outside the "
                    f"workspace: {target}")
        if _DD_TO_DEVICE.search(command):
            return GuardDecision(
                False, f"{BLOCK_REASON_DESTRUCTIVE}: dd writing to a device")
        if _SHUTDOWN.search(command):
            return GuardDecision(False, f"{BLOCK_REASON_DESTRUCTIVE}: system shutdown")
        return None

    def _check_sensitive(self, command: str) -> GuardDecision | None:
        for path in _ABSOLUTE_PATHS.findall(command):
            if _is_sensitive_path(path):
                return GuardDecision(
                    False, f"{BLOCK_REASON_SENSITIVE}: {path}")
        return None

    def _check_privilege(self, command: str) -> GuardDecision | None:
        first = command.split(None, 1)[0] if command else ""
        if first in {"sudo", "su"} or command in {"sudo", "su"}:
            return GuardDecision(False, f"{BLOCK_REASON_PRIVILEGE}: {first}")
        return None

    def _check_remote_pipe(self, command: str) -> GuardDecision | None:
        if _REMOTE_PIPE.search(command):
            return GuardDecision(False, BLOCK_REASON_REMOTE_PIPE)
        return None

    def _check_allowlist(self, command: str) -> GuardDecision:
        first = command.split(None, 1)[0] if command else ""
        base = PurePosixPath(first).name
        if base in self.allowlist:
            return GuardDecision(True)
        return GuardDecision(False,
                             f"{BLOCK_REASON_NOT_ALLOWLISTED}: {first or command!r}")


def _normalize(command: str) -> str:
    """Collapse whitespace, quotes, and common escapes for matching."""
    lowered = command.lower()
    lowered = re.sub(r"\s+", " ", lowered)
    lowered = lowered.replace('"', "").replace("'", "")
    lowered = lowered.replace("\\", "")
    return lowered.strip()


def _resolve_dots(path: str) -> str:
    """Resolve '.' and '..' segments lexically (no filesystem access)."""
    parts: list[str] = []
    for part in path.split("/"):
        if part == "" or part == ".":
            continue
        if part == ".." and parts:
            parts.pop()
        elif part != "..":
            parts.append(part)
    return "/" + "/".join(parts)


def _is_sensitive_path(path: str) -> bool:
    resolved = _resolve_dots(path.rstrip("/"))
    sensitive_files = {"/etc/shadow", "/etc/sudoers", "/etc/gshadow"}
    if resolved in sensitive_files:
        return True
    # Any path pointing inside a ~/.ssh directory (private keys).
    parts = resolved.split("/")
    if ".ssh" in parts[1:]:
        return True
    return False


def _recursive_rm_is_dangerous(target: str, workspace_root: str | None) -> bool:
    """A recursive rm is dangerous unless fully inside the workspace."""
    resolved = _resolve_dots(_strip_options_prefix(target))
    if resolved == "/":
        return True
    if workspace_root:
        root = _resolve_dots(workspace_root)
        if resolved != root and not resolved.startswith(root + "/"):
            return True
        return False  # inside the workspace: the model may clean its sandbox
    # No workspace root known: only allow relative (workspace-shaped) paths.
    return resolved.startswith("/")


def _strip_options_prefix(target: str) -> str:
    """Drop leading rm options like '-rf' or '--no-preserve-root'."""
    parts = target.split()
    while parts and parts[0].startswith("-"):
        parts = parts[1:]
    return parts[0] if parts else target


# rm with a recursive flag, capturing the first target path.
_RMRecursive = re.compile(r"\brm\s+(?:-[a-z]*r[a-z]*\s+|--recursive\s+)(?P<target>\S+)")
# dd writing directly to a raw device.
_DD_TO_DEVICE = re.compile(r"\bdd\b[^|]*\bof=/dev/")
# shutdown family.
_SHUTDOWN = re.compile(r"\b(shutdown|reboot|halt|poweroff|init\s+0|init\s+6)\b")
# classic fork bomb shapes.
_FORK_BOMB = re.compile(r":\(\)\s*\{.*\}\s*;?\s*:")
# curl/wget output piped into sh/bash.
_REMOTE_PIPE = re.compile(
    r"\b(curl|wget)\b[^|]*\|\s*(sudo\s+)?(ba|z|da|k)?sh\b")
# absolute paths, to feed sensitive-target checking.
_ABSOLUTE_PATHS = re.compile(r"(/[\w.\-/]+)")


def blocked_result(command: str, decision: GuardDecision) -> dict[str, Any]:
    """Structured error payload for a blocked command."""
    return {"error": {"type": "CommandBlocked",
                      "message": decision.reason or "command blocked"}}


def guard_for_runtime(runtime: "RuntimeConfig | None" = None,
                      workspace_root: str | None = None) -> CommandGuard:
    """Guard built from an explicit config.

    ``runtime=None`` (a directly constructed executor with no composition
    root) resolves the environment here, exactly once.
    """
    if runtime is None:
        from agent_runtime.runtime_config import RuntimeConfig

        runtime = RuntimeConfig.from_env()
    return CommandGuard(runtime.command_guard_mode,
                        workspace_root=workspace_root)


__all__ = [
    "BLOCK_REASON_DESTRUCTIVE", "BLOCK_REASON_NOT_ALLOWLISTED",
    "BLOCK_REASON_PRIVILEGE", "BLOCK_REASON_REMOTE_PIPE",
    "BLOCK_REASON_SENSITIVE", "CommandGuard", "GuardDecision",
    "blocked_result", "guard_for_runtime",
]
