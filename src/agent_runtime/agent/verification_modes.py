"""Verification finish-mode registry: OCP for submit gates.

:class:`TurnFinisher` used to branch on ``rules.mode == "off"`` in three
places, so adding a validation gate mode (e.g. ``advisory``) required
editing the core loop. Modes are now data: a
:class:`FinishModeDefinition` declares whether the submit tool is
intercepted, whether a plain-text answer may finish, and whether a
contract miss is reported. New modes register here without touching
``TurnFinisher``.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class FinishModeDefinition:
    """One verification mode's finish policy."""

    intercepts_submit: bool = True
    plain_answer_finishes: bool = False
    report_contract_miss: bool = True


_OFF = FinishModeDefinition(
    intercepts_submit=False,
    plain_answer_finishes=True,
    report_contract_miss=False,
)
_ENFORCED = FinishModeDefinition(
    intercepts_submit=True,
    plain_answer_finishes=False,
    report_contract_miss=True,
)


FINISH_MODES: dict[str, FinishModeDefinition] = {
    "off": _OFF,
    "return_contract": _ENFORCED,
    "task_result": _ENFORCED,
}


def register_finish_mode(name: str, definition: FinishModeDefinition) -> None:
    """Register one verification finish mode under ``name``."""
    FINISH_MODES[name] = definition


def get_finish_mode(mode: str) -> FinishModeDefinition:
    """Resolve ``mode`` to its finish policy.

    Unknown non-``off`` modes behave as enforced — preserving the
    historical ``mode != "off"`` semantics for harnesses that name a
    custom enforcing mode without registering it.
    """
    definition = FINISH_MODES.get(mode)
    if definition is not None:
        return definition
    if mode == "off":
        return _OFF
    return _ENFORCED


__all__ = [
    "FinishModeDefinition", "FINISH_MODES",
    "get_finish_mode", "register_finish_mode",
]
