"""Persistent multi-session state for the web UI.

A web UI session owns two things that the runtime cares about:

* the visible transcript (user bubbles, assistant prose, thinking
  blocks, tool cards), which is what the browser replays on reload, and
* the :class:`~agent_runtime.agent.Conversation` handed to
  :func:`~agent_runtime.agent.run_turn`, which is what keeps the agent's
  own context alive between turns.

Both are persisted as one JSON document per session so a restart of the
server does not lose the conversation.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from uuid import uuid4

from agent_runtime import storage
from agent_runtime.agent import Conversation


logger = logging.getLogger(__name__)


# Sentinel pushed into a subscriber queue to tell the stream to finish.
# It never reaches a browser: the stream stops before it is rendered.
STREAM_STOP: Any = object()


def default_sessions_root() -> Path:
    """Where sessions live when ``AGENT_RUNTIME_WEBUI_SESSIONS_DIR`` is unset.

    The default is the runtime home's ``sessions/`` directory — the one
    place the project persists anything (see
    :mod:`agent_runtime.storage`).  The env var still wins, so an
    existing deployment keeps its files where they are.
    """
    raw = os.getenv("AGENT_RUNTIME_WEBUI_SESSIONS_DIR")
    if raw:
        return Path(raw).expanduser()
    return storage.sessions_dir()


class FrameBus:
    """Fan out web UI frames to every browser watching a session.

    Frames carry a monotonic ``seq``. The bus keeps a short replay
    buffer so a browser that reconnects (or opens a session late) can
    catch up on what it missed instead of starting from a blank
    transcript.
    """

    def __init__(self, *, history: int = 500) -> None:
        self._seq = 0
        self._history: list[dict[str, Any]] = []
        self._history_limit = history
        self._subscribers: set[asyncio.Queue[Any]] = set()

    @property
    def seq(self) -> int:
        return self._seq

    def publish(self, frame: dict[str, Any]) -> None:
        """Publish one frame to every current subscriber."""
        self._seq += 1
        payload = {"seq": self._seq, **frame}
        self._history.append(payload)
        if len(self._history) > self._history_limit:
            del self._history[: len(self._history) - self._history_limit]
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(payload)
            except asyncio.QueueFull:  # pragma: no cover - unbounded queue
                pass

    def subscribe(self) -> "asyncio.Queue[Any]":
        queue: asyncio.Queue[Any] = asyncio.Queue()
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: "asyncio.Queue[Any]") -> None:
        self._subscribers.discard(queue)

    def replay(self, after: int = 0) -> list[dict[str, Any]]:
        """Frames newer than ``after``, oldest first."""
        return [frame for frame in self._history if frame.get("seq", 0) > after]

    def stop(self) -> None:
        """Close every open stream.

        Server-sent events never end on their own, so a browser that is
        still connected would keep uvicorn waiting during shutdown.
        The sentinel tells the stream to finish.
        """
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(STREAM_STOP)
            except asyncio.QueueFull:  # pragma: no cover - unbounded queue
                pass


@dataclass
class Session:
    """One browser conversation."""

    id: str
    title: str
    created_at: float
    updated_at: float
    segments: list[dict[str, Any]] = field(default_factory=list)
    messages: list[dict[str, Any]] = field(default_factory=list)
    conversation: Conversation = field(default_factory=Conversation)
    bus: FrameBus = field(default_factory=FrameBus)
    busy: bool = False
    task: Any = None

    @property
    def is_empty(self) -> bool:
        """Whether this session has never held a conversation.

        A session is created empty and only becomes real once the first
        message arrives.  Empty sessions are kept in memory (the browser
        needs one to type into) but are never persisted and never listed:
        otherwise every "new chat" click would leave a stub file behind
        and the sidebar would fill up with blank conversations.
        """
        return not self.segments and not self.messages

    def append_segment(self, segment: dict[str, Any]) -> None:
        """Add one transcript block and stamp the session as changed."""
        self.segments.append(segment)
        self.updated_at = time.time()

    def append_user_message(self, message: str) -> dict[str, Any]:
        """Record a user turn in the transcript and return the segment."""
        segment = {"id": f"{self.id}:user:{len(self.segments)}",
                   "type": "user", "content": message}
        self.append_segment(segment)
        if self.title == "New conversation":
            self.title = _derive_title(message)
        return segment

    def sync_messages(self) -> None:
        """Copy the runtime conversation into the persistable form."""
        self.messages = [dict(message) for message in self.conversation.messages]

    def summary(self) -> dict[str, Any]:
        return {"id": self.id, "title": self.title,
                "created_at": self.created_at, "updated_at": self.updated_at,
                "busy": self.busy}

    def to_record(self) -> dict[str, Any]:
        return {"id": self.id, "title": self.title,
                "created_at": self.created_at, "updated_at": self.updated_at,
                "segments": self.segments, "messages": self.messages}


def _derive_title(message: str) -> str:
    text = " ".join(message.split())
    return text[:48] + ("…" if len(text) > 48 else "")


class SessionStore:
    """Load, create, and persist web UI sessions."""

    def __init__(self, root: Path | None = None) -> None:
        self._root = Path(root) if root is not None else default_sessions_root()
        self._sessions: dict[str, Session] = {}

    @property
    def root(self) -> Path:
        return self._root

    def load(self) -> None:
        """Read every persisted session from disk.

        Records that never held a conversation are skipped: they are
        leftovers from an older build that wrote a file per "new chat"
        click, and loading them would resurrect the empty conversations
        the sidebar is meant to hide.
        """
        if not self._root.is_dir():
            return
        for path in sorted(self._root.glob("*.json")):
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                logger.warning("webui.session.load_failed path=%s error=%s",
                               path, exc)
                continue
            session = _session_from_record(record)
            if session is None:
                continue
            if session.is_empty:
                logger.debug("webui.session.load_skipped_empty path=%s", path)
                continue
            self._sessions[session.id] = session

    def list(self) -> list[Session]:
        """The sessions worth showing, newest first.

        Empty sessions are excluded: they exist only so the browser has
        somewhere to type, and listing them would fill the sidebar with
        blank conversations.
        """
        return sorted((session for session in self._sessions.values()
                       if not session.is_empty),
                      key=lambda session: session.updated_at, reverse=True)

    def shutdown(self) -> None:
        """Close every event stream and stop every running turn.

        Called when the server is asked to exit: the event streams are
        long-lived by design, so they have to be closed explicitly or
        the process waits for them forever.
        """
        for session in self._sessions.values():
            session.bus.stop()
            task = session.task
            if task is not None and not task.done():
                task.cancel()

    def get(self, session_id: str) -> Session | None:
        return self._sessions.get(session_id)

    def create(self, title: str | None = None) -> Session:
        """Start a session in memory.

        Nothing is written to disk yet: the session earns its file the
        moment it holds a conversation (see :meth:`save`).
        """
        now = time.time()
        session = Session(id=f"sess-{uuid4().hex[:12]}",
                          title=title or "New conversation",
                          created_at=now, updated_at=now)
        self._sessions[session.id] = session
        return session

    def delete(self, session_id: str) -> bool:
        session = self._sessions.pop(session_id, None)
        if session is None:
            return False
        path = self._path(session_id)
        try:
            path.unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:  # pragma: no cover - filesystem dependent
            logger.warning("webui.session.delete_failed path=%s error=%s",
                           path, exc)
        return True

    def rename(self, session_id: str, title: str) -> Session | None:
        session = self._sessions.get(session_id)
        if session is None:
            return None
        session.title = title.strip() or session.title
        session.updated_at = time.time()
        self.save(session)
        return session

    def save(self, session: Session) -> None:
        """Persist one session, ignoring filesystem failures.

        An empty session is not persisted at all.  It has no history
        worth keeping, and writing it would litter the sessions
        directory with one stub file per "new chat" click.
        """
        session.sync_messages()
        if session.is_empty:
            return
        path = self._path(session.id)
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(session.to_record(), ensure_ascii=False,
                                       indent=2, default=str),
                            encoding="utf-8")
        except OSError as exc:  # pragma: no cover - filesystem dependent
            logger.warning("webui.session.save_failed path=%s error=%s",
                           path, exc)

    def _path(self, session_id: str) -> Path:
        return self._root / f"{session_id}.json"


def _session_from_record(record: dict[str, Any]) -> Session | None:
    try:
        session = Session(id=str(record["id"]), title=str(record["title"]),
                          created_at=float(record.get("created_at", 0.0)),
                          updated_at=float(record.get("updated_at", 0.0)))
    except (KeyError, TypeError, ValueError):
        return None
    segments = record.get("segments")
    if isinstance(segments, list):
        session.segments = [segment for segment in segments
                            if isinstance(segment, dict)]
    messages = record.get("messages")
    if isinstance(messages, list):
        session.messages = [message for message in messages
                            if isinstance(message, dict)]
        session.conversation = Conversation()
        session.conversation.messages = list(session.messages)
    return session


__all__ = ["FrameBus", "Session", "SessionStore", "default_sessions_root"]
