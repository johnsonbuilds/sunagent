"""Expected turn failures that end a turn without raising to the caller.

A :class:`TurnFailure` carries a message that is already fit to return as
the turn's answer (an LLM error, an aborted finish fuse).  It lives in
its own module because both the turn loop and the turn finisher raise it;
keeping it in either would make the other import its caller.
"""

from __future__ import annotations


class TurnFailure(Exception):
    """An expected turn failure whose message becomes the turn's answer."""


__all__ = ["TurnFailure"]
