"""Return-contract gate for the verification gene.

labs-OO-Agents BenchAgent pattern as a typed tool call: the model must
submit solution_description / evidence / command_to_verify via the
submit_result tool when finishing. The harness checks *shape* (field
present plus minimal content) and *grounding* (evidence quotes observed
tool output; the declared command was actually executed; at least one
source edit was made via edit_file / apply_patch / write_file). It never executes gold tests here.
The declared command is model-authored per task, so the gate stays generic
across benchmarks; the finish instruction itself lives in the prompt gene
(harness ``prompt.system``) and the tool description, not here.
"""

from __future__ import annotations

import json
import re
from typing import Any, Mapping

MIN_FIELD_CHARS = 20
MIN_COMMAND_CHARS = 3
# Evidence must quote this many verbatim chars from one observation, or
# share this many significant tokens with observed outputs.
QUOTE_CHARS = 16
TOKEN_OVERLAP = 2

# Canonical submit_result parameters, in contract order. The free-text
# gate and the submit_result tool share these: a submission is rendered
# to the three-section answer shape before checking.
SUBMIT_FIELDS = ("solution_description", "evidence", "command_to_verify")
# Edit-necessity: a real fix necessarily writes; any attempted source edit
# via these tools counts (success is not parsed — the rerun gate checks
# the declared command actually passes).
EDIT_TOOLS = frozenset({"edit_file", "apply_patch", "write_file"})

# Generic boilerplate that proves nothing when shared between evidence and
# outputs; grounding requires *specific* overlap (names, numbers, lines).
_STOPWORDS = frozenset({
    "with", "from", "that", "this", "have", "were", "your", "observed",
    "output", "shell", "command", "result", "results", "evidence",
    "tests", "test", "passed", "failed", "passing", "failing", "success",
    "successful", "error", "errors", "failure", "failures",
})

_TOKEN_RE = re.compile(r"[a-z0-9][a-z0-9_\-\.]{4,}")


def _field_min_chars(field: str) -> int:
    # A legit verify command can be short ("pytest -q"); descriptions and
    # evidence may not.
    return MIN_COMMAND_CHARS if field.lower() == "command_to_verify" else MIN_FIELD_CHARS


def _field_value(fields: Mapping[str, Any], field: str) -> str:
    """Typed parameter value (schema guarantees strings; coerce defensively)."""
    raw = fields.get(field, "") if isinstance(fields, Mapping) else ""
    return raw if isinstance(raw, str) else str(raw)


def contract_nudge(gaps: dict[str, str] | list[str]) -> str:
    """User-channel retry message naming each failing contract field + why.

    Result + reason + contract shape only: no prescribed action — the
    model decides the next step from the failure named here.
    """
    if isinstance(gaps, dict):
        fields = "; ".join(f"{field} ({reason})" for field, reason in gaps.items())
    else:
        fields = ", ".join(gaps)
    return (
        f"Your submission fails the return contract: {fields}. "
        "submit_result requires: "
        "solution_description (root cause + fix), "
        "evidence (quote the actual shell output you observed: test names, counts, "
        "key lines — do not invent results), "
        "command_to_verify (one shell command you already ran that exits 0 on success). "
        "A finished fix must include at least one source edit with the right tool."
    )


def render_submission(fields: Mapping[str, Any]) -> str:
    """Render submit_result parameters to the three-section answer shape.

    The recorded answer keeps the established format so traces stay
    comparable across harness versions.
    """
    def value(key: str) -> str:
        raw = fields.get(key, "")
        return raw if isinstance(raw, str) else str(raw)

    return (f"solution_description: {value('solution_description')}\n"
            f"evidence: {value('evidence')}\n"
            f"command_to_verify: {value('command_to_verify')}")


def submit_nudge() -> str:
    """User-channel retry message: finish via the submit_result tool."""
    return (
        "To finish, call the submit_result tool (and only that tool) with all "
        "three parameters: solution_description (root cause + fix), "
        "evidence (quote the actual shell output you observed: test names, counts, "
        "key lines — do not invent results), "
        "command_to_verify (one shell command you already ran that exits 0 on success). "
        "Plain-text answers cannot finish the task. "
        "A finished fix must include at least one source edit with the right tool; "
        "run the tests first if you have not, then submit."
    )


def _significant_tokens(text: str) -> set[str]:
    return {tok for tok in _TOKEN_RE.findall(text.lower()) if tok not in _STOPWORDS}


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", text.lower()).strip()


def executed_commands(messages: list[Mapping[str, Any]]) -> list[str]:
    """``run_command`` command strings issued so far (canonical history)."""
    commands: list[str] = []
    for message in messages or []:
        if not isinstance(message, Mapping) or message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            if not isinstance(call, Mapping):
                continue
            function = call.get("function")
            if not isinstance(function, Mapping) or function.get("name") != "run_command":
                continue
            try:
                args = json.loads(function.get("arguments") or "{}")
            except (TypeError, json.JSONDecodeError, ValueError):
                continue
            command = args.get("command") if isinstance(args, dict) else None
            if isinstance(command, str) and command.strip():
                commands.append(command.strip())
    return commands


def has_source_edit(messages: list[Mapping[str, Any]] | None) -> bool:
    """One attempted ``edit_file`` / ``apply_patch`` / ``write_file`` call."""
    for message in messages or []:
        if not isinstance(message, Mapping) or message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            if not isinstance(call, Mapping):
                continue
            function = call.get("function")
            if isinstance(function, Mapping) and function.get("name") in EDIT_TOOLS:
                return True
    return False


def observation_texts(messages: list[Mapping[str, Any]]) -> list[str]:
    """Rendered ``role: tool`` observation contents seen so far."""
    texts: list[str] = []
    for message in messages or []:
        if not isinstance(message, Mapping) or message.get("role") != "tool":
            continue
        content = message.get("content")
        if isinstance(content, str) and content.strip():
            texts.append(content)
    return texts


def _evidence_grounded(evidence: str, observations: list[str]) -> bool:
    """Evidence quotes (verbatim span) or shares specific tokens with output."""
    norm_evidence = _normalize(evidence)
    if len(norm_evidence) < MIN_FIELD_CHARS:
        return False
    norm_observations = [_normalize(obs) for obs in observations
                         if len(_normalize(obs)) >= QUOTE_CHARS]
    # Short outputs quoted in full count immediately.
    if any(obs in norm_evidence for obs in norm_observations if len(obs) <= 64):
        return True
    # Otherwise any verbatim quote of QUOTE_CHARS from the evidence found
    # in output (step 4 guarantees a window lands inside any quoted run).
    for i in range(0, len(norm_evidence) - QUOTE_CHARS + 1, 4):
        span = norm_evidence[i:i + QUOTE_CHARS]
        if any(span in obs for obs in norm_observations):
            return True
    obs_tokens: set[str] = set()
    for obs in observations:
        obs_tokens |= _significant_tokens(obs)
    if not obs_tokens:
        return False
    return len(_significant_tokens(evidence) & obs_tokens) >= TOKEN_OVERLAP


def _command_grounded(command: str, executed: list[str]) -> bool:
    """Declared command matches a history run (exact or substring).

    Cheap pre-filter only: the rerun executes the declared string itself,
    so a miss here merely rejects without spending a verify-command execution.
    No program/token heuristics — those misfire on shell-prefixed commands.
    """
    norm = _normalize(command)
    if len(norm) < MIN_COMMAND_CHARS:
        return False
    return any(norm in _normalize(cmd) or _normalize(cmd) in norm
               for cmd in executed)


_QUOTED_SPAN_RE = re.compile(r"'[^']*'|\"[^\"]*\"")

_NARROW_RE = re.compile(r"(^|\s)(-k\b|--deselect\b|--ignore\b|/::)", re.IGNORECASE)
_FILE_TARGET_RE = re.compile(r"[\w\-/]+\.(?:py|sh)\b|tests?/[\w\-/.]+|test_[\w\-/]+")


def _is_file_level(command: str) -> bool:
    """Declared command targets test files, not a narrowed selection.

    ``-k`` / ``-m`` / ``--deselect`` / node-ids select a subset, so a
    passing rerun proves little about regressions. Only the last
    ``&&`` segment is checked so ``cd`` / ``source ... activate``
    prefixes stay allowed.
    """
    bare = _QUOTED_SPAN_RE.sub("", command)
    if _NARROW_RE.search(bare):
        return False
    last = bare.split("&&")[-1]
    return bool(_FILE_TARGET_RE.search(last))


def _masks_exit_code(command: str) -> bool:
    """Declared command hides the runner's exit code behind shell plumbing.

    ``|`` hands the exit code to the last pipe stage (tail/grep) and
    ``;`` / ``||`` let a trailing command succeed, so a failing suite
    looks green. ``&&`` chains are fail-closed (an early failure stops
    the chain nonzero) and ``>`` redirects keep the runner's own exit
    code, so both stay allowed. Quoted spans are ignored: a ``|`` inside
    a grep pattern is not a pipe.
    """
    bare = _QUOTED_SPAN_RE.sub("", command)
    return "|" in bare or ";" in bare


def check_submission(fields: Mapping[str, Any],
                     require: tuple[str, ...] | list[str],
                     messages: list[Mapping[str, Any]] | None = None) -> dict[str, str]:
    """Full gate on typed submit_result parameters: shape plus grounding.

    Returns ``{field: reason}`` for every failing field; empty means accept.
    Without trajectory (``messages=None``) only the shape check applies.
    Edit-necessity rejects edit-less finishes as a ``solution_description``
    gap: a real fix necessarily writes. The declared command must have been
    run, and — when the harness reruns — must exit 0 on the rerun. Which
    command counts as the verify command is the model's call per task
    (prompt contract); the gate verifies authenticity (run, passing,
    quoted), never the runner's identity.
    """
    gaps: dict[str, str] = {}
    for field in require or []:
        if len(_field_value(fields, field).strip()) < _field_min_chars(field):
            gaps[field] = "missing or too short"
    if messages is None:
        return gaps
    if "solution_description" in (require or []) and "solution_description" not in gaps:
        if not has_source_edit(messages):
            gaps["solution_description"] = (
                "no source edits yet — make a code change with "
                "edit_file/apply_patch/write_file before finishing")
    if "evidence" in (require or []) and "evidence" not in gaps:
        observations = observation_texts(messages)
        if not observations:
            gaps["evidence"] = "no tool output observed at all — run the tests first"
        elif not _evidence_grounded(_field_value(fields, "evidence"), observations):
            gaps["evidence"] = ("cites no observed tool output — quote actual lines "
                                "from a command you ran")
    if "command_to_verify" in (require or []) and "command_to_verify" not in gaps:
        executed = executed_commands(messages)
        command = _field_value(fields, "command_to_verify")
        if not executed:
            gaps["command_to_verify"] = "no shell command run yet — run it first"
        elif not _command_grounded(command, executed):
            gaps["command_to_verify"] = ("was never run — run that exact command "
                                          "before claiming it verifies the fix")
        elif _masks_exit_code(command):
            gaps["command_to_verify"] = (
                "output pipes mask the test exit code (the exit code "
                "becomes the pipe's, not the tests') — declare the command "
                "without pipes; redirect to a file if you need the log")
        elif not _is_file_level(command):
            gaps["command_to_verify"] = (
                "narrow/file-less command — declare a file-level test "
                "command, e.g. cd /testbed && pytest tests/test_auth.py "
                "-q --tb=short; -k/--deselect not allowed")
    return gaps


__all__ = ["MIN_FIELD_CHARS", "MIN_COMMAND_CHARS", "EDIT_TOOLS", "SUBMIT_FIELDS",
           "executed_commands", "has_source_edit", "observation_texts",
           "render_submission", "check_submission", "contract_nudge",
           "submit_nudge"]
