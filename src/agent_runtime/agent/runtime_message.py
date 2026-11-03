"""Provenance markers for user-role messages authored by the runtime.

The wire protocol only has ``user``, ``assistant``, ``system`` and ``tool``
roles.  Loop-internal nudges and observations therefore ride as ``user``
messages, but they are not task-turn boundaries.  Mark them explicitly so
context strategies can tell them apart without guessing from prose.
"""

from __future__ import annotations

from typing import Any

RUNTIME_ORIGIN = "runtime"


def runtime_user_message(content: str, *, kind: str) -> dict[str, Any]:
    """Build a user-role protocol message that is not a task boundary."""
    return {
        "role": "user",
        "content": content,
        "metadata": {"origin": RUNTIME_ORIGIN, "kind": kind},
    }


def is_runtime_user_message(message: dict[str, Any]) -> bool:
    """Whether ``message`` was authored by the runtime, not the user."""
    metadata = message.get("metadata")
    return (
        message.get("role") == "user"
        and isinstance(metadata, dict)
        and metadata.get("origin") == RUNTIME_ORIGIN
    )


__all__ = [
    "RUNTIME_ORIGIN",
    "is_runtime_user_message",
    "runtime_user_message",
]
