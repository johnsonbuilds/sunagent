"""The execute_code composite tool: write a script, run it, observe once.

This is the "code interpreter" execution pattern: instead of N round
trips (write file, run command, read output), the model ships one
complete script per tool call.  Intermediate data stays in workspace
files or in-memory variables; only what the script prints comes back.

Results are returned with a source-level output cap applied by the
executor (``MAX_OUTPUT_CHARS`` per stream, see
``agent_runtime.execution.base``) so a runaway print cannot OOM the
process; anything still oversized is spilled to ``.outputs/`` at the
transcript boundary (agent_runtime.agent.observations), so every
tool's bulky output is handled uniformly.
"""

from __future__ import annotations

import asyncio
import ast
import os
import posixpath
import re
import shlex
from typing import Any
from agent_runtime.execution.base import ShellExecutor, Workspace
from agent_runtime.execution.local import LocalShellExecutor, LocalWorkspace
from agent_runtime.execution.guard import GuardDecision


def _python_binary() -> str:
    return os.getenv("AGENT_RUNTIME_PYTHON", "python3")


LANGUAGE_SPECS: dict[str, dict[str, str]] = {
    "python": {"extension": "py",
               "interpreter": _python_binary,
               "install_hint": "install python3 (e.g. apt-get install -y python3)"},
    "bash": {"extension": "sh",
             "interpreter": "bash",
             "install_hint": ""},
    "r": {"extension": "R",
          "interpreter": "Rscript",
          "install_hint": "install R (e.g. apt-get install -y r-base-core)"},
    "node": {"extension": "js",
             "interpreter": "node",
             "install_hint": "install Node.js (e.g. apt-get install -y nodejs)"},
}
LANGUAGES = {name: spec["extension"] for name, spec in LANGUAGE_SPECS.items()}
SCRIPTS_DIR = ".scripts"
OUTPUTS_DIR = ".outputs"
DEFAULT_TIMEOUT = 120.0

_SEQUENCE = re.compile(r"^(\d{4})\.")

# Serializes auto-naming (list .scripts/ -> max+1 -> write) so concurrent
# execute_code calls cannot claim the same NNNN number.
_SCRIPT_LOCK = asyncio.Lock()

# Python footgun guardrails (mistake-proofing, not a security boundary:
# static AST checks are trivially bypassable by design — the real
# containment is OS-level isolation, not this list). Mirrors the
# labs-OO-Agents RestrictionsConfig subset that matters for scripts.
_BLOCKED_PY_MODULES = frozenset({"subprocess", "socket"})
_BLOCKED_PY_CALLS = frozenset({
    "os.system", "os.popen", "os.execv", "os.execve", "os.execl",
    "sys.exit", "os._exit", "os.abort", "os.kill",
})


def _interpreter_for(language: str) -> str:
    interpreter = LANGUAGE_SPECS[language]["interpreter"]
    return interpreter() if callable(interpreter) else interpreter


async def _next_script_name(workspace: Workspace, extension: str) -> str:
    """Continue the .scripts/NNNN.ext sequence across all languages."""
    listing = await workspace.list_dir(SCRIPTS_DIR)
    highest = 0
    for entry in listing.get("entries", []):
        match = _SEQUENCE.match(entry.get("name", ""))
        if match:
            highest = max(highest, int(match.group(1)))
    return f"{SCRIPTS_DIR}/{highest + 1:04d}.{extension}"


def _run_command_for(language: str, script: str) -> str:
    return f"{_interpreter_for(language)} {shlex.quote(script)}"


def validate_script_path(path: str) -> str | None:
    """Check an explicit ``execute_code(path=...)``; None means OK.

    Scripts must live under ``.scripts/``: the auto-naming default
    already does, and allowing arbitrary paths would let one tool call
    silently overwrite workspace source files. Returns the rejection
    reason, or None when the path is acceptable.
    """
    if not path or not path.strip():
        return "path must not be empty"
    norm = posixpath.normpath(path.strip().replace("\\", "/"))
    if posixpath.isabs(path.strip()) or norm == ".." or norm.startswith("../"):
        return f"path escapes the workspace: {path}"
    if norm != SCRIPTS_DIR and not norm.startswith(SCRIPTS_DIR + "/"):
        return (f"path must be under {SCRIPTS_DIR}/ "
                f"(got {path!r}); the default .scripts/NNNN.ext is preferred")
    return None


def _check_python_footguns(code: str) -> str | None:
    """Best-effort AST check for common Python footguns; None means OK.

    Guardrail, not a jail: ``open()`` alone gives arbitrary file I/O,
    so this list only catches what the model reaches for by mistake
    (process spawning, socket use, interpreter suicide). Unparseable
    code is left alone — the runtime will report the SyntaxError.
    """
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return None
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                top = (alias.name or "").split(".")[0]
                if top in _BLOCKED_PY_MODULES:
                    return (f"footgun: importing {alias.name!r} from a "
                            f"script; use run_command for process/network "
                            f"work instead")
        elif isinstance(node, ast.ImportFrom):
            top = (node.module or "").split(".")[0]
            if top in _BLOCKED_PY_MODULES:
                return (f"footgun: importing from {node.module!r} in a "
                        f"script; use run_command for process/network "
                        f"work instead")
        elif isinstance(node, ast.Call):
            func = node.func
            dotted: str | None = None
            if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
                dotted = f"{func.value.id}.{func.attr}"
            if dotted in _BLOCKED_PY_CALLS:
                return (f"footgun: {dotted}() in a script; "
                        f"run it via run_command instead")
    return None


def _guard_decision(executor: ShellExecutor, script: str,
                    language: str) -> GuardDecision | None:
    """Check the script's own text against the executor's guard.

    Bash scripts are checked line by line: the wrapper command
    (``bash .scripts/0001.sh``) is always harmless, so what needs
    vetting is the script content.  Python gets a best-effort AST
    footgun check (blocked process/network calls) — still a
    guardrail, not a security boundary: Python is Turing-complete,
    so any check on its source is trivially bypassed and must not
    block legitimate file work; the real boundary for code is the
    execution environment (hardened container vs local process).
    """
    guard = getattr(executor, "guard", None)
    if language == "bash":
        if guard is None:
            return None
        for line in script.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#"):
                continue
            decision = guard.check(stripped)
            if not decision.allowed:
                return decision
        return None
    if language == "python":
        reason = _check_python_footguns(script)
        if reason is not None:
            return GuardDecision(False, reason)
        return None
    return None


def _with_interpreter_hint(result: dict[str, Any],
                           language: str) -> dict[str, Any]:
    """Append an install hint when the interpreter was not found.

    Exit code 127 means the shell could not find the interpreter binary
    (e.g. ``Rscript: command not found`` on a machine without R).  Turn
    that opaque failure into an actionable hint so the agent can install
    the runtime via ``run_command`` and retry.
    """
    if result.get("exit_code") != 127:
        return result
    interpreter = _interpreter_for(language)
    hint = LANGUAGE_SPECS[language]["install_hint"]
    message = (f"hint: '{interpreter}' was not found; the script was not run."
               f" Install the {language} runtime first via run_command"
               + (f" ({hint})" if hint else ""))
    stderr = result.get("stderr") or ""
    if message not in stderr:
        result = {**result, "stderr": f"{stderr}\n{message}".strip()}
    return result


async def execute_code(code: str, language: str = "python",
                       path: str | None = None, timeout: float | None = DEFAULT_TIMEOUT,
                       *, workspace: Workspace | None = None,
                       executor: ShellExecutor | None = None) -> dict[str, Any]:
    """Write ``code`` to the workspace, execute it, return one result.

    Composes the Workspace and ShellExecutor protocols: the script is
    saved (default ``.scripts/NNNN.ext``, re-runnable later via
    ``run_command``), executed from the workspace root, and the command
    result is returned with ``script_path`` and ``language`` attached.
    """
    ws = workspace or LocalWorkspace()
    ex = executor or LocalShellExecutor()
    if language not in LANGUAGES:
        return {"stdout": "", "stderr": "", "exit_code": None,
                "duration": 0.0,
                "error": {"type": "UnsupportedLanguage",
                          "message": f"unsupported language: {language!r} "
                                     f"(choose from {sorted(LANGUAGES)})"},
                "language": language}

    if path is not None:
        reason = validate_script_path(path)
        if reason is not None:
            return {"stdout": "", "stderr": "", "exit_code": None,
                    "duration": 0.0,
                    "error": {"type": "InvalidPath", "message": reason},
                    "script_path": path, "language": language}
        script = path
        written = await ws.write_file(script, code)
    else:
        async with _SCRIPT_LOCK:
            script = await _next_script_name(ws, LANGUAGES[language])
            written = await ws.write_file(script, code)
    if "error" in written:
        return {**written, "script_path": script, "language": language}

    cwd = str(ws.root) if ws.root is not None else None
    command = _run_command_for(language, script)
    decision = _guard_decision(ex, code, language)
    if decision is not None and not decision.allowed:
        return {"stdout": "", "stderr": "", "exit_code": None,
                "duration": 0.0,
                "error": {"type": "CommandBlocked",
                          "message": decision.reason or "command blocked"},
                "script_path": script, "language": language}
    result = await ex.execute(command, cwd=cwd, timeout=timeout)
    result = _with_interpreter_hint(result, language)

    # Source-level truncation happens in the executor (MAX_OUTPUT_CHARS
    # per stream); oversized output still reaching here is spilled to
    # .outputs/ once at the transcript boundary
    # (agent_runtime.agent.observations), which handles every tool's
    # bulky results uniformly.  Returning the result as-is keeps tool
    # handlers policy-free.
    return {**result, "script_path": script, "language": language}


__all__ = [
    "DEFAULT_TIMEOUT", "LANGUAGES", "LANGUAGE_SPECS",
    "OUTPUTS_DIR", "SCRIPTS_DIR", "execute_code", "validate_script_path",
]
