"""Shared workspace layout constants (re-export).

Canonical definitions live in :mod:`agent_runtime.execution.base` so
both ``agent`` (observation spilling) and ``tools`` (execute_code /
read_output) import downward to the execution layer instead of
``agent`` importing a concrete tool implementation module.
This module re-exports them for ``agent_runtime.tools.paths`` importers.
"""

from agent_runtime.execution.base import OUTPUTS_DIR, SCRIPTS_DIR

__all__ = ["OUTPUTS_DIR", "SCRIPTS_DIR"]
