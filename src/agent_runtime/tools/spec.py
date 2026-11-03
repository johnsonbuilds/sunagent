"""Tool specification value object.

Owned here (leaf module) so individual tool implementation modules
(``files.py``, ``patch.py``, ...) can build their own ``ToolSpec``
without importing the registry aggregator (``tools.py``) — that would
be a circular import.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any


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


__all__ = ["ToolSpec"]
