"""Small, composable building blocks for learning agent systems."""

from typing import TYPE_CHECKING, Any

from .events import AgentEvent, EventEmitter

if TYPE_CHECKING:
    from .trace import RunEvent, RunTrace

__all__ = ["AgentEvent", "EventEmitter", "RunEvent", "RunTrace"]


def __getattr__(name: str) -> Any:
    """Keep the root import compatible without loading trace eagerly."""
    if name in {"RunEvent", "RunTrace"}:
        from .trace import RunEvent, RunTrace
        return {"RunEvent": RunEvent, "RunTrace": RunTrace}[name]
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
