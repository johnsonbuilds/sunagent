"""Tool registry and the built-in tools used by the runtime.

Schemas live with each tool implementation module (``files.py``,
``patch.py``, ...) — this module only aggregates them into a registry.
Adding a tool means adding its ``*_spec`` factory next to its handler
plus one line in :func:`builtin_tool_specs`.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Protocol

from agent_runtime.execution.base import ShellExecutor, Workspace
from agent_runtime.execution.local import LocalShellExecutor, LocalWorkspace
from agent_runtime.tools.spec import ToolSpec

from .code import execute_code_spec
from .files import (
    edit_file_spec,
    list_dir_spec,
    read_file_spec,
    read_output_spec,
    write_file_spec,
)
from .patch import apply_patch_spec
from .search import find_files_spec, grep_search_spec
from .shell import run_command_spec
from .submit import submit_result, submit_result_spec
from .symbols import TreeSitterIndex, find_references_spec, find_symbol_spec


class ToolExecutor(Protocol):
    """What the agent loop needs from a tool layer: the workspace its file
    tools are bound to, schemas to advertise, and ``execute`` to run one
    call.  :class:`ToolRegistry` is the built-in implementation; tests and
    integrations may supply their own.

    ``workspace`` is a required part of the contract: observation
    spilling must target the same storage the file tools read and write,
    so the runtime never guesses it from incidental attributes, and it is
    never absent.
    """

    workspace: Workspace

    @property
    def schemas(self) -> list[dict[str, Any]]: ...

    async def execute(self, name: str, arguments: Mapping[str, Any]) -> Any: ...


class ToolRegistry:
    def __init__(self, specs: list[ToolSpec] | None = None, *,
                 workspace: Workspace | None = None) -> None:
        self._tools: dict[str, ToolSpec] = {}
        for spec in specs or []:
            self.register(spec)
        # The registry owns the shared workspace: file tools bind it, and
        # harness layers (e.g. observation spilling) read it from here so
        # there is exactly one workspace instance per run.  A registry
        # built without one gets a local workspace now, so ``workspace``
        # is always present and spilling never has to guess.
        self.workspace = workspace if workspace is not None else LocalWorkspace()

    @property
    def schemas(self) -> list[dict[str, Any]]:
        return [spec.schema for spec in self._tools.values()]

    def register(self, spec: ToolSpec) -> None:
        import inspect

        if not inspect.iscoroutinefunction(spec.handler):
            raise TypeError(f"Tool handler must be async: {spec.name}")
        self._tools[spec.name] = spec

    async def execute(self, name: str, arguments: Mapping[str, Any]) -> Any:
        try:
            spec = self._tools[name]
        except KeyError as exc:
            raise ValueError(f"Unknown tool: {name}") from exc
        return await spec.handler(**dict(arguments))


def builtin_tool_specs(executor: ShellExecutor | None = None,
                       workspace: Workspace | None = None) -> list[ToolSpec]:
    """Every tool the runtime knows how to build."""
    shell_executor = executor if executor is not None else LocalShellExecutor()
    file_workspace = workspace if workspace is not None else LocalWorkspace()
    symbol_index = TreeSitterIndex(file_workspace)
    return [
        run_command_spec(shell_executor, file_workspace),
        write_file_spec(file_workspace),
        read_file_spec(file_workspace),
        read_output_spec(file_workspace),
        list_dir_spec(file_workspace),
        edit_file_spec(file_workspace),
        apply_patch_spec(file_workspace),
        grep_search_spec(file_workspace),
        find_files_spec(file_workspace),
        find_symbol_spec(symbol_index),
        find_references_spec(symbol_index),
        execute_code_spec(shell_executor, file_workspace),
        submit_result_spec(),
    ]


def create_default_registry(executor: ShellExecutor | None = None,
                            enabled: list[str] | None = None,
                            workspace: Workspace | None = None) -> ToolRegistry:
    """Build the tool registry, optionally filtered by the harness gene.

    ``enabled=None`` keeps every built-in tool; otherwise the registry
    exposes exactly the named tools (plus the always-on ``read_output``
    pager), in the given order.  ``workspace`` becomes the registry's
    shared workspace; when omitted a default one is created here and
    carried on the registry, so every tool and the observation spiller
    share one instance.
    """
    shell_executor = executor if executor is not None else LocalShellExecutor()
    file_workspace = workspace if workspace is not None else LocalWorkspace()
    specs = builtin_tool_specs(shell_executor, file_workspace)
    if enabled is None:
        return ToolRegistry(specs, workspace=file_workspace)
    by_name = {spec.name: spec for spec in specs}
    unknown = [name for name in enabled if name not in by_name]
    if unknown:
        raise ValueError(f"Unknown tools in harness: {unknown}")
    # read_output is infrastructure, not a harness gene: the observation
    # spiller writes references under .outputs/ that only this tool can
    # page, so it ships with every harness regardless of the tool list.
    names = list(dict.fromkeys([*enabled, "read_output"]))
    return ToolRegistry([by_name[name] for name in names],
                        workspace=file_workspace)


__all__ = [
    "ToolExecutor", "ToolRegistry", "ToolSpec",
    "builtin_tool_specs", "create_default_registry", "submit_result",
]
