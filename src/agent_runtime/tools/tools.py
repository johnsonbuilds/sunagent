"""Tool registry and the built-in tools used by the runtime."""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from functools import partial
import inspect
from typing import Any, Protocol

from agent_runtime.execution.base import ShellExecutor, Workspace
from agent_runtime.execution.local import LocalShellExecutor, LocalWorkspace

from .code import execute_code
from .files import DEFAULT_READ_LIMIT, edit_file, list_dir, read_file, read_output, write_file
from .patch import apply_patch
from .search import DEFAULT_GREP_RESULTS, DEFAULT_FIND_RESULTS, find_files, grep_search
from .shell import run_command
from .symbols import TreeSitterIndex, find_references, find_symbol


class ToolExecutor(Protocol):
    """What the agent loop needs from a tool layer: schemas to advertise
    and ``execute`` to run one call.  :class:`ToolRegistry` is the
    built-in implementation; tests and integrations may supply their own.
    """

    @property
    def schemas(self) -> list[dict[str, Any]]: ...

    async def execute(self, name: str, arguments: Mapping[str, Any]) -> Any: ...


@dataclass(frozen=True)
class ToolSpec:
    name: str
    description: str
    parameters: dict[str, Any]
    handler: Callable[..., Awaitable[Any]]

    @property
    def schema(self) -> dict[str, Any]:
        return {"type": "function", "function": {
            "name": self.name, "description": self.description,
            "parameters": self.parameters,
        }}


class ToolRegistry:
    def __init__(self, specs: list[ToolSpec] | None = None, *,
                 workspace: Workspace | None = None) -> None:
        self._tools: dict[str, ToolSpec] = {}
        for spec in specs or []:
            self.register(spec)
        # The registry owns the shared workspace: file tools bind it, and
        # harness layers (e.g. observation spilling) read it from here so
        # there is exactly one workspace instance per run.
        self.workspace = workspace

    @property
    def schemas(self) -> list[dict[str, Any]]:
        return [spec.schema for spec in self._tools.values()]

    def register(self, spec: ToolSpec) -> None:
        if not inspect.iscoroutinefunction(spec.handler):
            raise TypeError(f"Tool handler must be async: {spec.name}")
        self._tools[spec.name] = spec

    async def execute(self, name: str, arguments: Mapping[str, Any]) -> Any:
        try:
            spec = self._tools[name]
        except KeyError as exc:
            raise ValueError(f"Unknown tool: {name}") from exc
        return await spec.handler(**dict(arguments))


def _run_command_spec(executor: ShellExecutor,
                      workspace: Workspace | None = None) -> ToolSpec:
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


def _write_file_spec(workspace: Workspace) -> ToolSpec:
    return ToolSpec("write_file",
                    "Create a new file or replace the entire content of an "
                    "existing file. Provide the complete intended file content "
                    "in one call; parent directories are created automatically. "
                    "This tool is for scenarios where the whole file content "
                    "is being defined at once: new-file creation and full-file "
                    "rewrites.",
                    {"type": "object", "properties": {
                        "path": {"type": "string",
                                 "description": "File path inside the workspace"},
                        "content": {"type": "string",
                                    "description": "Complete file content"}},
                     "required": ["path", "content"]},
                    partial(write_file, workspace=workspace))


def _read_file_spec(workspace: Workspace) -> ToolSpec:
    return ToolSpec("read_file",
                    "Read a file as text. Long files are returned one page of "
                    "lines at a time; continue with a larger offset when the "
                    "result reports truncated: true.",
                    {"type": "object", "properties": {
                        "path": {"type": "string",
                                 "description": "File path inside the workspace"},
                        "offset": {"type": "integer",
                                   "description": "First line to return (1-based)",
                                   "default": 1},
                        "limit": {"type": "integer",
                                  "description": "Maximum number of lines to return",
                                  "default": DEFAULT_READ_LIMIT}},
                     "required": ["path"]},
                    partial(read_file, workspace=workspace))


def _read_output_spec(workspace: Workspace) -> ToolSpec:
    return ToolSpec("read_output",
                    "Page through a tool output that was saved under .outputs/ "
                    "when it exceeded the transcript budget (the observation "
                    "names the file). Returns one page of lines at a time; "
                    "continue with a larger offset when the result reports "
                    "truncated: true. Never re-run a tool just to see its "
                    "output again — page the saved file instead.",
                    {"type": "object", "properties": {
                        "path": {"type": "string",
                                 "description": "Path under .outputs/, e.g. "
                                                "'.outputs/<name>.txt' as named "
                                                "by the observation"},
                        "offset": {"type": "integer",
                                   "description": "First line to return (1-based)",
                                   "default": 1},
                        "limit": {"type": "integer",
                                  "description": "Maximum number of lines to return",
                                  "default": DEFAULT_READ_LIMIT}},
                     "required": ["path"]},
                    partial(read_output, workspace=workspace))


def _list_dir_spec(workspace: Workspace) -> ToolSpec:
    return ToolSpec("list_dir",
                    "List the entries of a directory (directories first, "
                    "then files).",
                    {"type": "object", "properties": {
                        "path": {"type": "string",
                                 "description": "Directory path inside the workspace",
                                 "default": "."}},
                     "required": []},
                    partial(list_dir, workspace=workspace))


def _edit_file_spec(workspace: Workspace) -> ToolSpec:
    return ToolSpec("edit_file",
                    "Replace exactly one occurrence of old_str with new_str in a file."
                    "old_str must match uniquely (exact first, tolerant fallbacks "
                    "for line-number prefixes and indentation; "
                    "the applied mode is reported back as match_mode). "
                    "Zero or ambiguous matches raise with a rendered diagnosis. "
                    "Edited content that no longer parses (Python, JSON, YAML, "
                    "shell) is rejected and nothing is written. "
                    "Best suited for single-point edits and iterative small "
                    "changes, including text containing conflict markers.",
                    {"type": "object", "properties": {
                        "path": {"type": "string",
                                 "description": "File path inside the workspace"},
                        "old_str": {"type": "string",
                                    "description": "Text to replace, must match "
                                                   "uniquely (exact preferred; "
                                                   "line-number-prefix-stripped "
                                                   "and indent-insensitive "
                                                   "fallbacks allowed)"},
                        "new_str": {"type": "string",
                                    "description": "Replacement text (empty to delete)"}},
                     "required": ["path", "old_str", "new_str"]},
                    partial(edit_file, workspace=workspace))


def _apply_patch_spec(workspace: Workspace) -> ToolSpec:
    return ToolSpec("apply_patch",
                    "Make batched text replacements using SEARCH/REPLACE blocks.\n"
                    "\n"
                    "This tool accepts exactly one argument: \"patch\", a string.\n"
                    "Put all file paths, SEARCH text, and replacement text inside\n"
                    "that single string. Do not send them as separate arguments.\n"
                    "\n"
                    "This tool is for scenarios where multiple replacements are\n"
                    "applied together as one atomic unit with all-or-nothing "
                    "validation.\n"
                    "Every SEARCH section must match its file exactly once; "
                    "zero or ambiguous matches fail the whole patch and "
                    "nothing is written. Edited content that no longer parses "
                    "(Python, JSON, YAML, shell) is rejected the same way.\n"
                    "\n"
                    "Format per block:\n"
                    "path/to/file.py\n"
                    "<<<<<<< SEARCH\n"
                    "old_context copied from the file, with indentation,\n"
                    "including enough surrounding lines to match one location\n"
                    "=======\n"
                    "new_context to replace it with\n"
                    ">>>>>>> REPLACE\n"
                    "\n"
                    "Put each path and each delimiter on its own line, using a bare\n"
                    "path without tags or quotes. End every block with >>>>>>> REPLACE.\n"
                    "Empty SEARCH creates a new file (fails when the file already "
                    "exists; use a non-empty SEARCH section to edit it); "
                    "empty replacement deletes "
                    "the match. Text containing conflict-marker lines cannot be "
                    "expressed here — use the single-point edit path for such "
                    "edits instead.",
                    {"type": "object", "properties": {
                        "patch": {"type": "string",
                                  "description": "The entire patch as one "
                                                 "string, including all file "
                                                 "paths and SEARCH/REPLACE "
                                                 "blocks. Every block must "
                                                 "end with >>>>>>> REPLACE. "
                                                 "Do not split this into "
                                                 "multiple arguments."}},
                     "required": ["patch"],
                     "additionalProperties": False},
                    partial(apply_patch, workspace=workspace))


def _grep_search_spec(workspace: Workspace) -> ToolSpec:
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


def _find_files_spec(workspace: Workspace) -> ToolSpec:
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


def _find_symbol_spec(index: TreeSitterIndex) -> ToolSpec:
    return ToolSpec("find_symbol",
                    "Find where a Python class or function is defined "
                    "(.py files only), using "
                    "fault-tolerant tree-sitter parsing (works even in "
                    "files with syntax errors). Matches an exact short or "
                    "qualified "
                    "name like 'ClassName.method' and returns "
                    "(path, name, kind, line, end_line) for read_file.",
                    {"type": "object", "properties": {
                        "name": {"type": "string",
                                 "description": "Symbol name (short or "
                                                "qualified)"},
                        "kind": {"type": "string", "enum": ["class", "function",
                                                            "method"],
                                 "description": "Filter by symbol kind"}},
                     "required": ["name"]},
                    partial(find_symbol, index=index))


def _find_references_spec(index: TreeSitterIndex) -> ToolSpec:
    return ToolSpec("find_references",
                    "Find identifier usages of a simple Python name across "
                    ".py files (tree-sitter based textual match, up to 200 "
                    "results with a truncated flag), excluding its "
                    "definition sites. Dotted or qualified names are not "
                    "resolved. Returns (path, line, preview) per "
                    "reference.",
                    {"type": "object", "properties": {
                        "name": {"type": "string",
                                 "description": "Simple identifier to look for "
                                                "(Python only; dotted names "
                                                "are not resolved)"}},
                     "required": ["name"]},
                    partial(find_references, index=index))


def _execute_code_spec(executor: ShellExecutor, workspace: Workspace) -> ToolSpec:
    return ToolSpec("execute_code",
                    "Write a complete script to the workspace and execute it "
                    "in one step, returning exit code, stdout, and stderr. "
                    "Supports python (runs via python3 by default, "
                    "AGENT_RUNTIME_PYTHON override), bash (runs via bash), "
                    "r (runs via Rscript), and node (runs via node); the "
                    "interpreter for the chosen language must already be "
                    "installed. Prefer this over run_command whenever the "
                    "step needs loops, branches, multi-step data processing, "
                    "or error handling: keep intermediate data in files or "
                    "variables and print only the final result. Each script "
                    "is saved under .scripts/ and can be re-run with "
                    "run_command after fixing the environment.",
                    {"type": "object", "properties": {
                        "code": {"type": "string",
                                 "description": "Complete script content"},
                        "language": {"type": "string", "enum": ["python", "bash", "r", "node"],
                                     "description": "Script language: python "
                                                    "(python3 by default, "
                                                    "AGENT_RUNTIME_PYTHON override), "
                                                    "bash, r (Rscript), "
                                                    "or node (node). Defaults to "
                                                    "python — pass language "
                                                    "explicitly for anything else",
                                     "default": "python"},
                        "path": {"type": "string",
                                 "description": "Script path inside the workspace; "
                                                "defaults to .scripts/NNNN.ext"},
                        "timeout": {"type": "number",
                                    "description": "Timeout in seconds",
                                    "default": 120}},
                     "required": ["code"]},
                    partial(execute_code, workspace=workspace, executor=executor))


async def submit_result(solution_description: str = "",
                        evidence: str = "",
                        command_to_verify: str = "") -> dict[str, Any]:
    """Finish-signal placeholder; the agent loop intercepts this tool.

    Under a ``task_result`` verification harness the loop validates the
    three parameters (shape, grounding, declared-command rerun) before
    this handler could ever run. Reaching here means the harness does
    not honor the tool, so report that instead of pretending to finish.
    """
    return {"submitted": False,
            "hint": ("submit_result is only honored under a task_result "
                     "verification harness; state your final answer as text.")}


def _submit_result_spec() -> ToolSpec:
    return ToolSpec(
        "submit_result",
        "Declare the task finished with a structured result. Call this tool, "
        "and only this tool, when the task is complete and verified. "
        "solution_description: states the root cause and what was changed. "
        "evidence: quote the actual shell output you observed; do not invent results."
        "command_to_verify: one shell command you already ran, that exits 0 on success, "
        "must name test files (file-level, e.g. pytest tests/test_auth.py -q --tb=short), "
        "no -k/--deselect narrowing, "
        "including any leading cd. Keep test output concise (`-q --tb=short`, no tail pipes hiding failures). "
        "Never declare a command with output pipes (`|`, `;`, `||`): pipes hand the exit code"
        " to the last stage, so a failing suite looks green. `&&` chains and `>` redirects are fine."
        "Example: solution_description: fixed missing URL-encoding in auth.py with quote_plus()."
        "evidence: pytest -q passed: 128 passed in 4.3s. command_to_verify: cd /testbed && pytest tests/test_auth.py -q --tb=short.",
        {"type": "object", "properties": {
            "solution_description": {"type": "string",
                                     "description": "Root cause and fix",
                                     "minLength": 20},
            "evidence": {"type": "string",
                         "description": "Quoted shell output observed",
                         "minLength": 20},
            "command_to_verify": {"type": "string",
                                   "description": "Test command "
                                                  "already run, exits 0."
                                                  "Keep test output concise (-q --tb=short, no tail pipes hiding failures). ",
                                  "minLength": 3}},
         "required": ["solution_description", "evidence"],
         "additionalProperties": False},
        submit_result)


def builtin_tool_specs(executor: ShellExecutor | None = None,
                       workspace: Workspace | None = None) -> list[ToolSpec]:
    """Every tool the runtime knows how to build."""
    shell_executor = executor if executor is not None else LocalShellExecutor()
    file_workspace = workspace if workspace is not None else LocalWorkspace()
    symbol_index = TreeSitterIndex(file_workspace)
    return [
        _run_command_spec(shell_executor, file_workspace),
        _write_file_spec(file_workspace),
        _read_file_spec(file_workspace),
        _read_output_spec(file_workspace),
        _list_dir_spec(file_workspace),
        _edit_file_spec(file_workspace),
        _apply_patch_spec(file_workspace),
        _grep_search_spec(file_workspace),
        _find_files_spec(file_workspace),
        _find_symbol_spec(symbol_index),
        _find_references_spec(symbol_index),
        _execute_code_spec(shell_executor, file_workspace),
        _submit_result_spec(),
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
