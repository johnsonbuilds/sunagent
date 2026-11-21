"""Persistent multi-session state for the web UI.

A session owns the visible transcript and the agent conversation, both
persisted as one JSON document per session so a server restart does not
lose the conversation.
"""

from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from threading import RLock
from typing import Any
from uuid import uuid4

from agent_runtime import storage
from agent_runtime.agent import Conversation


logger = logging.getLogger(__name__)


STREAM_STOP: Any = object()


def default_sessions_root() -> Path:
    """Where sessions live when ``AGENT_RUNTIME_WEBUI_SESSIONS_DIR`` is unset."""
    raw = os.getenv("AGENT_RUNTIME_WEBUI_SESSIONS_DIR")
    if raw:
        return Path(raw).expanduser()
    return storage.sessions_dir()


class FrameBus:
    """Fan out web UI frames to every browser watching a session.

    Frames carry a monotonic ``seq`` and a short replay buffer lets a
    reconnecting browser catch up instead of starting from a blank
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
        """Close every open stream."""
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
    # Catalog id and harness id, not endpoint settings: the secret never
    # reaches a session file. Both are resolved to a per-turn snapshot
    # when a turn starts (see ``webui.turn_context``).
    model: str | None = None
    harness: str | None = None
    # Which surface started the turns of this session.  Inherited by every
    # turn (see ``webui.turn_context``) so a diagnosis follow-up does not
    # get counted as ordinary web UI traffic.
    origin: str = "webui"
    # What a diagnosis session is about: ``{"type": "task|run|cohort", ...}``.
    # Empty for ordinary conversations.  This is the only task -> diagnosis
    # link; diagnosis never writes to the board event stream.
    target: dict[str, Any] | None = None
    # The first report of a diagnosis session.  Follow-up turns may append
    # new assistant segments, so the evaluation page must read this id
    # rather than "the last assistant segment".
    report_segment_id: str | None = None
    # Fingerprint of the evidence bundle the first turn was built from and
    # the id of that bundle under ``<home>/diagnosis/``.
    bundle_hash: str | None = None
    diagnosis_id: str | None = None

    @property
    def is_empty(self) -> bool:
        """Whether this session has never held a conversation."""
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

    @property
    def harness_locked(self) -> bool:
        """Whether the harness choice is frozen for this session."""
        return not self.is_empty

    def mark_report_segment(self, segment_id: str) -> bool:
        """Freeze the diagnosis report on its first assistant segment.

        A follow-up question appends more assistant segments; the report
        the evaluation page shows must stay the first one, so later calls
        are ignored.  A blank id is refused instead of frozen, because it
        would point at nothing and never be overwritten.
        """
        if self.report_segment_id:
            return False
        text = segment_id.strip() if isinstance(segment_id, str) else ""
        if not text:
            return False
        self.report_segment_id = text
        return True

    def summary(self) -> dict[str, Any]:
        return {"id": self.id, "title": self.title,
                "created_at": self.created_at, "updated_at": self.updated_at,
                "busy": self.busy, "model": self.model,
                "harness": self.harness, "harness_locked": self.harness_locked,
                "origin": self.origin, "target": self.target,
                "report_segment_id": self.report_segment_id}

    def to_record(self) -> dict[str, Any]:
        return {"id": self.id, "title": self.title,
                "created_at": self.created_at, "updated_at": self.updated_at,
                "segments": self.segments, "messages": self.messages,
                "model": self.model, "harness": self.harness,
                "origin": self.origin, "target": self.target,
                "report_segment_id": self.report_segment_id,
                "bundle_hash": self.bundle_hash,
                "diagnosis_id": self.diagnosis_id}


def _derive_title(message: str) -> str:
    text = " ".join(message.split())
    return text[:48] + ("…" if len(text) > 48 else "")


def _copy_target(target: dict[str, Any] | None) -> dict[str, Any] | None:
    """Detach a diagnosis target from its caller and from the JSON cache.

    Targets nest (a cohort carries a ``filters`` object), so a shallow
    copy would let a later edit leak into the reloaded cache.  An empty
    target is the same as none.
    """
    if not isinstance(target, dict) or not target:
        return None
    return copy.deepcopy(target)


class SessionStore:
    """Load, create, and persist web UI sessions.

    Queries run inside HTTP worker threads (``asyncio.to_thread``) while
    the event loop keeps creating and deleting sessions, so the dict is
    guarded by a lock the same way ``BoardStore`` guards its cards.
    """

    def __init__(self, root: Path | None = None) -> None:
        self._root = Path(root) if root is not None else default_sessions_root()
        self._sessions: dict[str, Session] = {}
        self._lock = RLock()

    @property
    def root(self) -> Path:
        return self._root

    def load(self) -> None:
        """Read every persisted session from disk."""
        if not self._root.is_dir():
            return
        loaded: dict[str, Session] = {}
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
            loaded[session.id] = session
        with self._lock:
            self._sessions.update(loaded)

    def list(self) -> list[Session]:
        """The sessions worth showing, newest first."""
        with self._lock:
            sessions = [session for session in self._sessions.values()
                        if not session.is_empty]
        return sorted(sessions, key=lambda session: session.updated_at,
                      reverse=True)

    def shutdown(self) -> None:
        """Close every event stream and stop every running turn."""
        with self._lock:
            sessions = list(self._sessions.values())
        for session in sessions:
            session.bus.stop()
            task = session.task
            if task is not None and not task.done():
                task.cancel()

    def get(self, session_id: str) -> Session | None:
        with self._lock:
            return self._sessions.get(session_id)

    def create(self, title: str | None = None, *,
               model: str | None = None,
               harness: str | None = None,
               origin: str = "webui",
               target: dict[str, Any] | None = None,
               diagnosis_id: str | None = None,
               bundle_hash: str | None = None) -> Session:
        """Start a session in memory.

        The id carries a random suffix: the timestamp alone collides when
        two sessions are created within the same second.
        """
        now = time.time()
        session = Session(
            id="sess-" + time.strftime("%Y%m%d-%H%M%S", time.localtime(now))
            + "-" + uuid4().hex[:6],
            title=title or "New conversation",
            created_at=now, updated_at=now,
            model=model, harness=harness,
            origin=origin, target=_copy_target(target),
            diagnosis_id=diagnosis_id, bundle_hash=bundle_hash,
        )
        with self._lock:
            self._sessions[session.id] = session
        return session

    def delete(self, session_id: str) -> bool:
        with self._lock:
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
        with self._lock:
            session = self._sessions.get(session_id)
        if session is None:
            return None
        session.title = title.strip() or session.title
        session.updated_at = time.time()
        self.save(session)
        return session

    def save(self, session: Session) -> None:
        """Persist one session, ignoring filesystem failures."""
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
    model = record.get("model")
    if isinstance(model, str) and model.strip():
        session.model = model.strip()
    harness = record.get("harness")
    if isinstance(harness, str) and harness.strip():
        session.harness = harness.strip()
    origin = record.get("origin")
    if isinstance(origin, str) and origin.strip():
        session.origin = origin.strip()
    target = record.get("target")
    if isinstance(target, dict):
        session.target = _copy_target(target)
    for name in ("report_segment_id", "bundle_hash", "diagnosis_id"):
        value = record.get(name)
        if isinstance(value, str) and value.strip():
            setattr(session, name, value.strip())
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
