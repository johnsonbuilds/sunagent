"""Finish-signal placeholder tool.

Under a ``task_result`` verification harness the agent loop intercepts
this tool; reaching the handler means the harness does not honor it.
"""

from __future__ import annotations

from typing import Any

from agent_runtime.tools.spec import ToolSpec


async def submit_result(solution_description: str = "",
                        evidence: str = "",
                        command_to_verify: str = "") -> dict[str, Any]:
    """Finish-signal placeholder; the agent loop intercepts this tool."""
    return {"submitted": False,
            "hint": ("submit_result is only honored under a task_result "
                     "verification harness; state your final answer as text.")}


def submit_result_spec() -> ToolSpec:
    return ToolSpec(
        "submit_result",
        "Declare the task finished with a structured result. Call this tool, "
        "and only this tool, when the task is complete and verified. "
        "solution_description: states the root cause and what was changed. "
        "evidence: quote the actual shell output you observed; do not invent results."
        "command_to_verify: one shell command you already ran, that exits 0 on success, "
        "must name test files (file-level, e.g. pytest tests/test_auth.py -q --tb=short), "
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


__all__ = ["submit_result", "submit_result_spec"]
