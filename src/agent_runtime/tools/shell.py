"""Shell execution tool with structured, non-throwing command results."""

from __future__ import annotations

from typing import Any

from agent_runtime.execution.base import ShellExecutor, Workspace
from agent_runtime.execution.local import LocalShellExecutor
from agent_runtime.tools.spec import ToolSpec
from functools import partial


async def run_command(command: str, cwd: str | None = None,
                timeout: float | None = 30.0,
                executor: ShellExecutor | None = None,
                default_cwd: str | None = None) -> dict[str, Any]:
    """Run a shell command without raising for command-level failures.

    A non-zero exit code is not an exception. Process startup failures and
    timeouts are represented by the optional structured ``error`` field.
    Duration is measured in seconds.  ``default_cwd`` is the working
    directory used when the call does not pass ``cwd`` — the registry
    binds it to the workspace root so shell commands and file tools
    share one notion of "the workspace".
    """
    shell_executor = executor if executor is not None else LocalShellExecutor()
    return await shell_executor.execute(
        command, cwd if cwd is not None else default_cwd, timeout)


def run_command_spec(executor: ShellExecutor,
                      workspace: Workspace | None = None) -> ToolSpec:
    """ToolSpec for ``run_command``; schema lives with the implementation."""
    default_cwd = (str(workspace.root) if workspace is not None
                   and workspace.root is not None else None)
    return ToolSpec("run_command",
                    "Run a shell command and return its output and exit code. "
                    "Commands run in the workspace root unless cwd is given. "
                    "A non-zero exit code is a normal result, not an exception; "
                    "blocked commands and timeouts are reported via the "
                    "error field.",
                    {"type": "object", "properties": {
                        "command": {"type": "string", "description": "Shell command to run"},
                        "cwd": {"type": "string",
                                "description": "Working directory for the command "
                                               "(defaults to the workspace root)"},
                        "timeout": {"type": "number", "description": "Timeout in seconds",
                                    "default": 30}},
                     "required": ["command"]},
                    partial(run_command, executor=executor, default_cwd=default_cwd))


__all__ = ["run_command", "run_command_spec"]
