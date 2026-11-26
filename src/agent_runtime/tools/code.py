"""The execute_code composite tool: write a script, run it, observe once.

One complete script per tool call instead of N round trips; intermediate
data stays in workspace files or variables and only printed output comes
back.  The executor caps each stream (``MAX_OUTPUT_CHARS``) and anything
still oversized is spilled to ``.outputs/`` at the transcript boundary.
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
from agent_runtime.execution.base import OUTPUTS_DIR, SCRIPTS_DIR
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

# Models routinely pass the interpreter name ("python3") instead of the
# canonical key; aliasing avoids a wasted tool call.
LANGUAGE_ALIASES: dict[str, str] = {
    "python": "python", "python3": "python", "py": "python", "py3": "python",
    "bash": "bash", "sh": "bash", "shell": "bash",
    "r": "r", "rscript": "r",
    "node": "node", "nodejs": "node", "js": "node", "javascript": "node",
}

_C_HINT = ("C/C++ is not a script language here; write the source with "
           "write_file and compile/run it with run_command "
           "(e.g. cc -O2 foo.c -o foo && ./foo)")
_UNSUPPORTED_LANGUAGE_HINTS: dict[str, str] = {
    "python2": "python2 is not supported; use python (python3) or run_command",
    "c": _C_HINT, "cc": _C_HINT, "gcc": _C_HINT,
    "cpp": _C_HINT, "c++": _C_HINT, "cxx": _C_HINT,
}
DEFAULT_TIMEOUT = 120.0

_SEQUENCE = re.compile(r"^(\d{4})\.")

# Serializes auto-naming so concurrent calls cannot claim the same NNNN.
_SCRIPT_LOCK = asyncio.Lock()

# Mistake-proofing, not a security boundary: static AST checks are
# trivially bypassable; real containment is OS-level isolation.
_BLOCKED_PY_MODULES = frozenset({"subprocess", "socket"})
_BLOCKED_PY_CALLS = frozenset({
    "os.system", "os.popen", "os.execv", "os.execve", "os.execl",
    "sys.exit", "os._exit", "os.abort", "os.kill",
})


def _interpreter_for(language: str) -> str:
    interpreter = LANGUAGE_SPECS[language]["interpreter"]
    return interpreter() if callable(interpreter) else interpreter


def normalize_language(language: str) -> str:
    """Return the canonical language key for ``language``.

    Languages that cannot be handled by a single interpreter invocation
    (C/C++) or that would change semantics (python2) get an actionable
    error instead of a wrong alias.
    """
    key = (language or "").strip().lower()
    hint = _UNSUPPORTED_LANGUAGE_HINTS.get(key)
    if hint is not None:
        raise ValueError(f"execute_code: {hint}")
    canonical = LANGUAGE_ALIASES.get(key)
    if canonical is None:
        raise ValueError(
            f"execute_code: unsupported language: {language!r} "
            f"(choose from {sorted(LANGUAGES)}; "
            f"aliases: python3/py, sh, js, Rscript)")
    return canonical


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


def _is_scripts_path(path: str) -> bool:
    return path == SCRIPTS_DIR or path.startswith(SCRIPTS_DIR + "/")


def validate_script_path(path: str, root: Any = None) -> tuple[str | None, str | None]:
    """Check and normalize an explicit ``execute_code(path=...)``.

    Scripts must live under ``.scripts/`` so one tool call cannot
    overwrite workspace source files.  Inside that boundary the resolver
    is forgiving: a bare filename is placed under ``.scripts/`` and an
    absolute path inside a known root is relativized.

    Returns ``(script, reason)``; ``reason`` distinguishes a true
    workspace escape from a mere convention violation.
    """
    if not path or not path.strip():
        return None, "path must not be empty"
    raw = path.strip().replace("\\", "/")
    if raw.startswith("~"):
        return None, "path must be relative to the workspace (no ~ expansion)"
    candidate = posixpath.normpath(raw)
    if candidate in (".", ".."):
        return None, (f"path must name a file under {SCRIPTS_DIR}/ "
                       f"(got {path!r})")
    if posixpath.isabs(raw):
        if root is None:
            return None, (f"use a workspace-relative path under {SCRIPTS_DIR}/ "
                           f"(got {path!r}); absolute paths are not accepted "
                           f"without a workspace root")
        norm_root = posixpath.normpath(str(root).replace("\\", "/"))
        prefix = norm_root.rstrip("/") + "/"
        if candidate != norm_root and not candidate.startswith(prefix):
            return None, f"path escapes the workspace: {path}"
        candidate = posixpath.relpath(candidate, norm_root)
        if candidate == ".":
            return None, (f"path must name a file under {SCRIPTS_DIR}/ "
                           f"(got {path!r})")
        if not _is_scripts_path(candidate):
            return None, (f"scripts must live under {SCRIPTS_DIR}/ "
                           f"(got {path!r}); use "
                           f"{SCRIPTS_DIR}/{posixpath.basename(candidate)}")
    else:
        if candidate == ".." or candidate.startswith("../"):
            return None, f"path escapes the workspace: {path}"
        if candidate == SCRIPTS_DIR:
            return None, (f"path must name a file under {SCRIPTS_DIR}/, "
                           f"not the directory")
        if "/" not in candidate:
            candidate = posixpath.join(SCRIPTS_DIR, candidate)
        elif not _is_scripts_path(candidate):
            return None, (f"scripts must live under {SCRIPTS_DIR}/ "
                           f"(got {path!r}); use "
                           f"{SCRIPTS_DIR}/{candidate.lstrip('./')}")
    if candidate == SCRIPTS_DIR:
        return None, (f"path must name a file under {SCRIPTS_DIR}/, "
                       f"not the directory")
    return candidate, None


def ensure_script_extension(script: str,
                            language: str) -> tuple[str | None, str | None]:
    """Append the language extension when missing; reject a mismatch.

    A misleading extension breaks the "re-run with run_command" contract.
    """
    expected = LANGUAGES[language]
    _, ext = posixpath.splitext(script)
    if not ext:
        return f"{script}.{expected}", None
    if ext.lstrip(".").lower() != expected.lower():
        return None, (f"path extension {ext!r} does not match language "
                       f"{language!r} (expected .{expected})")
    return script, None


async def _script_exists(workspace: Workspace, script: str) -> bool:
    """Whether an explicit script path already exists in the workspace."""
    parent = posixpath.dirname(script) or "."
    listing = await workspace.list_dir(parent)
    if "error" in listing:
        return False
    return any(entry.get("name") == posixpath.basename(script)
               for entry in listing.get("entries", []))


def _check_python_footguns(code: str) -> str | None:
    """Best-effort AST check for common Python footguns; None means OK.

    Catches what the model reaches for by mistake (process spawning,
    socket use, interpreter suicide).  Unparseable code is left alone —
    the runtime reports the SyntaxError.
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

    The wrapper command (``bash .scripts/0001.sh``) is always harmless,
    so bash is vetted line by line.  Python gets the AST footgun check —
    a guardrail only; the real boundary is the execution environment.
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
    """Append an install hint when the interpreter was not found (exit 127)."""
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

    The script is saved (default ``.scripts/NNNN.ext``, re-runnable via
    ``run_command``) and executed from the workspace root.
    """
    ws = workspace or LocalWorkspace()
    ex = executor or LocalShellExecutor()
    language = normalize_language(language)

    overwritten = False
    if path is not None and path.strip():
        script, reason = validate_script_path(path, ws.root)
        if reason is not None or script is None:
            raise ValueError(
                f"execute_code: {reason or 'invalid script path'}")
        script, ext_reason = ensure_script_extension(script, language)
        if ext_reason is not None or script is None:
            raise ValueError(
                f"execute_code: {ext_reason or 'invalid script path'}")
        overwritten = await _script_exists(ws, script)
        written = await ws.write_file(script, code)
    else:
        async with _SCRIPT_LOCK:
            script = await _next_script_name(ws, LANGUAGES[language])
            written = await ws.write_file(script, code)
    if "error" in written:
        return {**written, "script_path": script, "language": language,
                "overwritten": overwritten}

    cwd = str(ws.root) if ws.root is not None else None
    command = _run_command_for(language, script)
    decision = _guard_decision(ex, code, language)
    if decision is not None and not decision.allowed:
        return {"stdout": "", "stderr": "", "exit_code": None,
                "duration": 0.0,
                "error": {"type": "CommandBlocked",
                          "message": decision.reason or "command blocked"},
                "script_path": script, "language": language,
                "overwritten": overwritten}
    result = await ex.execute(command, cwd=cwd, timeout=timeout)
    result = _with_interpreter_hint(result, language)

    # Truncation is the executor's job; spill-to-.outputs/ happens once at
    # the transcript boundary, so handlers stay policy-free.
    return {**result, "script_path": script, "language": language,
            "overwritten": overwritten}


def execute_code_spec(executor: ShellExecutor, workspace: Workspace) -> ToolSpec:
    """ToolSpec for ``execute_code``; schema lives with the implementation."""
    from functools import partial

    from agent_runtime.tools.spec import ToolSpec

    return ToolSpec("execute_code",
                    "Write a complete script to the workspace and execute it "
                    "in one step, returning exit code, stdout, and stderr. "
                    "Supports python (runs via python3 by default, "
                    "AGENT_RUNTIME_PYTHON override; aliases python3/py), "
                    "bash (alias sh), r (alias Rscript), and node "
                    "(aliases js/javascript). "
                    "C/C++ are not supported here: write the source with "
                    "write_file and compile/run it with run_command. "
                    "Missing interpreters are reported with an install hint; "
                    "install the runtime via run_command and retry. "
                    "Local execution has no sandbox (the command guard only "
                    "catches mistakes, it is not a security boundary); "
                    "untrusted code needs an externally hardened container. "
                    "Prefer this over run_command whenever the "
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
                                                    "(Python 3; aliases "
                                                    "python3, py), bash "
                                                    "(alias sh), r (alias "
                                                    "Rscript), or node "
                                                    "(aliases js, javascript). "
                                                    "Defaults to python. "
                                                    "C/C++ are not supported "
                                                    "here; use write_file + "
                                                    "run_command.",
                                     "default": "python"},
                        "path": {"type": "string",
                                 "description": "Optional script path. A bare "
                                                "filename (e.g. foo.py) is "
                                                "placed under .scripts/; "
                                                ".scripts/... and in-workspace "
                                                "absolute paths under "
                                                ".scripts/ are accepted. Other "
                                                "workspace paths are rejected. "
                                                "An explicit path may overwrite "
                                                "an existing file; the result "
                                                "reports overwritten=true. "
                                                "Defaults to .scripts/NNNN.ext"},
                        "timeout": {"type": "number",
                                    "description": "Timeout in seconds",
                                    "default": 120}},
                     "required": ["code"]},
                    partial(execute_code, workspace=workspace, executor=executor))


__all__ = [
    "DEFAULT_TIMEOUT", "LANGUAGES", "LANGUAGE_ALIASES", "LANGUAGE_SPECS",
    "OUTPUTS_DIR", "SCRIPTS_DIR", "ensure_script_extension", "execute_code",
    "normalize_language", "validate_script_path", "execute_code_spec",
]
