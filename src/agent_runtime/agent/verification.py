"""Return-contract gate for the verification gene.

labs-OO-Agents BenchAgent pattern adapted to a free-text turn:
the model must declare solution_description / evidence / command_to_verify
in its final answer. The harness checks *shape* (field present plus minimal
content) and *grounding* (evidence quotes observed tool output; the declared
command was actually executed; at least one source edit was made via
edit_file / apply_patch / write_file). It never executes gold tests here. The
declared command is model-authored per task, so the gate stays generic
across benchmarks; the finish instruction itself lives in the prompt gene
(harness ``prompt.system``), not here.
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


def _parse_json_object(answer: str) -> dict[str, Any] | None:
    """Return the first JSON object found in the answer, if any."""
    try:
        data = json.loads(answer)
        if isinstance(data, dict):
            return data
    except (json.JSONDecodeError, ValueError):
        pass
    for match in re.finditer(r"\{.*?\}", answer, re.DOTALL):
        try:
            data = json.loads(match.group(0))
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(data, dict):
            return data
    return None


def _section_content(answer_lower: str, field: str) -> str:
    """Text following the field key (up to the next required key or EOF)."""
    idx = answer_lower.find(field.lower())
    if idx < 0:
        return ""
    return answer_lower[idx + len(field):idx + len(field) + 800]


def extract_field_value(answer: str, field: str) -> str:
    """Best-effort value of one contract field (JSON-aware, else section)."""
    answer = answer or ""
    parsed = _parse_json_object(answer)
    if parsed is not None:
        lowered = {str(key).lower(): str(value) for key, value in parsed.items()}
        return lowered.get(field.lower(), "").strip()
    content = _section_content(answer.lower(), field)
    return re.sub(r"^[\s:#*\-`\"']+", "", content).strip()


def missing_fields(answer: str, require: tuple[str, ...] | list[str]) -> list[str]:
    """Fields whose key is absent or whose section is too thin to count."""
    if not require:
        return []
    missing = []
    for field in require:
        if len(extract_field_value(answer, field)) < _field_min_chars(field):
            missing.append(field)
    return missing


def contract_nudge(gaps: dict[str, str] | list[str]) -> str:
    """User-channel retry message naming each failing contract field + why."""
    if isinstance(gaps, dict):
        fields = "; ".join(f"{field} ({reason})" for field, reason in gaps.items())
    else:
        fields = ", ".join(gaps)
    return (
        f"Your final answer fails the return contract: {fields}. "
        "Reply with all required fields: solution_description (root cause + fix), "
        "evidence (quote the actual shell output you observed: test names, counts, "
        "key lines — do not invent results), "
        "command_to_verify (one shell command you already ran that exits 0 on success). "
        "A finished fix must include at least one source edit "
        "(edit_file/apply_patch/write_file). "
        "Run the tests first if you have not; then restate the answer."
    )


def render_submission(fields: Mapping[str, Any]) -> str:
    """Render submit_result parameters to the three-section answer shape.

    Lets tool submissions reuse the free-text gate verbatim (shape plus
    grounding), and keeps the recorded answer in the established format
    so traces stay comparable across harness modes.
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
        "A finished fix must include at least one source edit; "
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


def match_executed_command(command: str,
                           executed: list[str]) -> str | None:
    """The history command grounding the declared one, if any.

    Same rule as the gate (substring or program + detail): the declared
    string may carry trailing prose or fences from free-text answers, so
    the rerun executes the canonical history command it matches — clean
    and with known exit behavior — instead of the raw declared string.
    """
    norm = _normalize(command)
    if len(norm) < MIN_COMMAND_CHARS:
        return None
    norm_executed = [(_normalize(cmd), cmd) for cmd in executed]
    for norm_cmd, raw in norm_executed:
        if norm in norm_cmd or norm_cmd in norm:
            return raw
    program = norm.split()[0] if norm.split() else ""
    wanted = _significant_tokens(norm)
    for norm_cmd, raw in norm_executed:
        cmd_tokens = _significant_tokens(norm_cmd)
        if program and program in norm_cmd.split():
            # Tiny commands ("pytest -q") pass on the program; detailed
            # ones must share detail, or "pytest tests/other.py" would
            # pass off a "pytest tests/login.py" run.
            if len(wanted) < 2 or len(wanted & cmd_tokens) >= 2:
                return raw
    return None


def _command_grounded(command: str, executed: list[str]) -> bool:
    """Declared command was actually run (substring or program + detail)."""
    return match_executed_command(command, executed) is not None


def check_contract(answer: str, require: tuple[str, ...] | list[str],
                   messages: list[Mapping[str, Any]] | None = None) -> dict[str, str]:
    """Full gate: shape per field plus evidence/command/edit grounding.

    Returns ``{field: reason}`` for every failing field; empty means accept.
    Without trajectory (``messages=None``) only the shape check applies.
    Edit-necessity rejects edit-less finishes as a ``solution_description``
    gap: a real fix necessarily writes, so quoting output and naming a
    command you ran is not enough (psf-1142 / pytest-10051 class).
    """
    gaps: dict[str, str] = {}
    for field in require or []:
        if len(extract_field_value(answer, field)) < _field_min_chars(field):
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
        elif not _evidence_grounded(extract_field_value(answer, "evidence"), observations):
            gaps["evidence"] = ("cites no observed tool output — quote actual lines "
                                "from a command you ran")
    if "command_to_verify" in (require or []) and "command_to_verify" not in gaps:
        executed = executed_commands(messages)
        if not executed:
            gaps["command_to_verify"] = "no shell command run yet — run it first"
        elif not _command_grounded(extract_field_value(answer, "command_to_verify"),
                                   executed):
            gaps["command_to_verify"] = ("was never run — run that exact command "
                                         "before claiming it verifies the fix")
    return gaps


__all__ = ["MIN_FIELD_CHARS", "MIN_COMMAND_CHARS", "EDIT_TOOLS", "SUBMIT_FIELDS",
           "missing_fields", "extract_field_value", "executed_commands",
           "has_source_edit", "match_executed_command", "observation_texts",
           "render_submission", "check_contract", "contract_nudge",
           "submit_nudge"]
