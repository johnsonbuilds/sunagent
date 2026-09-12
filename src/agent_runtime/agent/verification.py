"""Return-contract gate for the verification gene.

labs-OO-Agents BenchAgent pattern adapted to a free-text turn:
the model must declare solution_description / evidence / command_to_verify
in its final answer. The harness checks *shape only* — field present plus
minimal content — and never executes gold tests here. The declared command
is model-authored per task, so the gate stays generic across benchmarks.
"""

from __future__ import annotations

import json
import re
from typing import Any

MIN_FIELD_CHARS = 20


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
    return answer_lower[idx + len(field):idx + len(field) + 500]


def missing_fields(answer: str, require: tuple[str, ...] | list[str]) -> list[str]:
    """Fields whose key is absent or whose section is too thin to be evidence."""
    if not require:
        return []
    answer = answer or ""
    parsed = _parse_json_object(answer)
    if parsed is not None:
        lowered = {str(key).lower(): str(value) for key, value in parsed.items()}
        missing: list[str] = []
        for field in require:
            value = lowered.get(field.lower(), "")
            if len(value.strip()) < MIN_FIELD_CHARS:
                missing.append(field)
        return missing
    answer_lower = answer.lower()
    missing = []
    for field in require:
        content = _section_content(answer_lower, field)
        if not content:
            missing.append(field)
            continue
        # Strip the key's own separators (":", "#", "*", whitespace) so a
        # bare "evidence:" header with nothing after it does not pass.
        stripped = re.sub(r"^[\s:#*\-`\"']+", "", content).strip()
        if len(stripped) < MIN_FIELD_CHARS:
            missing.append(field)
    return missing


def contract_nudge(missing: list[str]) -> str:
    """User-channel retry message listing the absent contract fields."""
    fields = ", ".join(missing)
    return (
        f"Your final answer is missing the return contract fields: {fields}. "
        "Reply with all required fields: solution_description (root cause + fix), "
        "evidence (actual shell output you observed, not a guess), "
        "command_to_verify (a shell command that exits 0 on success). "
        "Do not call further tools unless you need fresh evidence first."
    )


__all__ = ["MIN_FIELD_CHARS", "missing_fields", "contract_nudge"]
