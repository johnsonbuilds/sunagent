"""Persistent task board for the web UI.

The board is deliberately independent of the conversation store: a task
exists before it has a conversation, and a conversation may outlive the
task that created it.  The only link is an optional ``session_id`` on a
task, resolved through :class:`webui.sessions.SessionStore` when a
snapshot is rendered.

Everything is stored in one JSON document under the runtime home
(``workspace/board.json`` by default).  The file is small, a single user
edits it at a time, and moves need whole-column consistency, so one
atomic snapshot is simpler and safer than one file per task.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from threading import RLock
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from agent_runtime import storage

if TYPE_CHECKING:  # pragma: no cover - import cycle only for type checkers
    from .sessions import SessionStore


logger = logging.getLogger(__name__)

BOARD_VERSION = 1
BOARD_STATUSES = ("backlog", "todo", "doing", "review", "done")
BOARD_PRIORITIES = ("low", "normal", "high", "urgent")
PRIORITY_WEIGHT = {"low": 0, "normal": 1, "high": 2, "urgent": 3}

TITLE_MAX = 200
DESCRIPTION_MAX = 20_000
TAG_MAX = 32
TAGS_MAX = 20
ERROR_MAX = 2_000
CHOICE_MAX = 200
COMMENT_MAX = 10_000
RESPONSE_MAX = 100_000
RUN_STATUSES = ("completed", "error", "cancelled")

_STATUS_INDEX = {status: index for index, status in enumerate(BOARD_STATUSES)}


class BoardError(Exception):
    """Base class for board failures."""


class BoardValidationError(BoardError):
    """The request would create an invalid task."""


class BoardNotFoundError(BoardError):
    """The requested task does not exist."""


class BoardStorageError(BoardError):
    """The board could not be written to disk."""


def _as_float(value: Any) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _as_optional_float(value: Any) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_optional_text(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text[:ERROR_MAX] if text else None


def _clean_title(value: Any) -> str:
    if not isinstance(value, str):
        raise BoardValidationError("title must be a string")
    title = value.strip()
    if not title:
        raise BoardValidationError("title must not be empty")
    if len(title) > TITLE_MAX:
        raise BoardValidationError(
            f"title must be at most {TITLE_MAX} characters")
    return title


def _clean_description(value: Any) -> str:
    if not isinstance(value, str):
        raise BoardValidationError("description must be a string")
    description = value.strip()
    if not description:
        raise BoardValidationError("description must not be empty")
    if len(description) > DESCRIPTION_MAX:
        raise BoardValidationError(
            f"description must be at most {DESCRIPTION_MAX} characters")
    return description


def _clean_error(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text[:ERROR_MAX] if text else None


def _load_choice(value: Any) -> str | None:
    """Read a model/harness id from disk, clamping it like other fields."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    return text[:CHOICE_MAX] if text else None


def _clean_choice(value: Any, field: str) -> str | None:
    """Validate a model/harness id supplied by a client.

    ``None`` and the empty string both mean "use the service default".
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise BoardValidationError(f"{field} must be a string")
    text = value.strip()
    if not text:
        return None
    if len(text) > CHOICE_MAX:
        raise BoardValidationError(
            f"{field} must be at most {CHOICE_MAX} characters")
    return text


def _clean_comment(value: Any) -> str:
    """Validate one user comment before it enters the board."""
    if not isinstance(value, str):
        raise BoardValidationError("comment must be a string")
    body = value.strip()
    if not body:
        raise BoardValidationError("comment must not be empty")
    if len(body) > COMMENT_MAX:
        raise BoardValidationError(
            f"comment must be at most {COMMENT_MAX} characters")
    return body


def _clean_response(value: Any) -> str:
    """Keep the agent's final answer as-is, bounded for the board file."""
    if not isinstance(value, str):
        return ""
    return value[:RESPONSE_MAX]


def _load_comments(value: Any) -> list[dict[str, Any]]:
    """Read persisted comments, skipping entries we cannot trust."""
    if not isinstance(value, list):
        return []
    comments: list[dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, dict):
            continue
        body = raw.get("body")
        if not isinstance(body, str) or not body.strip():
            continue
        comment_id = raw.get("id")
        if not isinstance(comment_id, str) or not comment_id.strip():
            comment_id = f"comment-{uuid4().hex[:12]}"
        comments.append({
            "id": comment_id.strip(),
            "body": body.strip()[:COMMENT_MAX],
            "created_at": _as_float(raw.get("created_at")),
            "run_id": _load_choice(raw.get("run_id")),
        })
    return comments


def _load_runs(value: Any) -> list[dict[str, Any]]:
    """Read persisted run history, skipping entries we cannot trust."""
    if not isinstance(value, list):
        return []
    runs: list[dict[str, Any]] = []
    for raw in value:
        if not isinstance(raw, dict):
            continue
        run_id = raw.get("run_id")
        if not isinstance(run_id, str) or not run_id.strip():
            continue
        status = raw.get("status")
        if status not in RUN_STATUSES:
            status = "error"
        runs.append({
            "run_id": run_id.strip(),
            "started_at": _as_optional_float(raw.get("started_at")),
            "finished_at": _as_float(raw.get("finished_at")),
            "status": status,
            "response": _clean_response(raw.get("response")),
            "error": _clean_error(raw.get("error")),
        })
    return runs


def _clean_status(value: Any) -> str:
    if value not in BOARD_STATUSES:
        raise BoardValidationError(
            "status must be one of " + ", ".join(BOARD_STATUSES))
    return value


def _clean_priority(value: Any) -> str:
    if value not in BOARD_PRIORITIES:
        raise BoardValidationError(
            "priority must be one of " + ", ".join(BOARD_PRIORITIES))
    return value


def _clean_tags(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise BoardValidationError("tags must be a list of strings")
    tags: list[str] = []
    for raw in value:
        if not isinstance(raw, str):
            raise BoardValidationError("tags must be a list of strings")
        tag = raw.strip()
        if not tag:
            continue
        if len(tag) > TAG_MAX:
            raise BoardValidationError(
                f"each tag must be at most {TAG_MAX} characters")
        if tag not in tags:
            tags.append(tag)
    if len(tags) > TAGS_MAX:
        raise BoardValidationError(
            f"a task may have at most {TAGS_MAX} tags")
    return tags


@dataclass
class BoardTask:
    """One card on the board."""

    id: str
    title: str
    description: str = ""
    status: str = "backlog"
    priority: str = "normal"
    tags: list[str] = field(default_factory=list)
    comments: list[dict[str, Any]] = field(default_factory=list)
    runs: list[dict[str, Any]] = field(default_factory=list)
    position: int = 0
    model: str | None = None
    harness: str | None = None
    session_id: str | None = None
    queued_at: float | None = None
    run_id: str | None = None
    started_at: float | None = None
    finished_at: float | None = None
    last_error: str | None = None
    created_at: float = 0.0
    updated_at: float = 0.0

    def to_record(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "status": self.status,
            "priority": self.priority,
            "tags": list(self.tags),
            "comments": [dict(comment) for comment in self.comments],
            "runs": [dict(run) for run in self.runs],
            "position": self.position,
            "model": self.model,
            "harness": self.harness,
            "session_id": self.session_id,
            "queued_at": self.queued_at,
            "run_id": self.run_id,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "last_error": self.last_error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_record(cls, record: Any) -> "BoardTask | None":
        """Read one persisted task, skipping records we cannot trust."""
        if not isinstance(record, dict):
            return None
        task_id = record.get("id")
        if not isinstance(task_id, str) or not task_id.strip():
            return None
        try:
            title = _clean_title(record.get("title"))
        except BoardValidationError:
            return None
        status = record.get("status", "backlog")
        if status not in BOARD_STATUSES:
            return None
        priority = record.get("priority", "normal")
        if priority not in BOARD_PRIORITIES:
            priority = "normal"
        description = record.get("description", "")
        if not isinstance(description, str):
            description = ""
        description = description[:DESCRIPTION_MAX]
        tags: list[str] = []
        raw_tags = record.get("tags")
        if isinstance(raw_tags, list):
            for raw in raw_tags:
                if not isinstance(raw, str):
                    continue
                tag = raw.strip()
                if tag and len(tag) <= TAG_MAX and tag not in tags:
                    tags.append(tag)
                if len(tags) >= TAGS_MAX:
                    break
        position = record.get("position", 0)
        try:
            position = max(0, int(position))
        except (TypeError, ValueError):
            position = 0
        session_id = record.get("session_id")
        if not isinstance(session_id, str) or not session_id.strip():
            session_id = None
        created_at = _as_float(record.get("created_at"))
        updated_at = _as_float(record.get("updated_at")) or created_at
        return cls(id=task_id.strip(), title=title, description=description,
                   status=status, priority=priority, tags=tags,
                   comments=_load_comments(record.get("comments")),
                   runs=_load_runs(record.get("runs")),
                   position=position,
                   model=_load_choice(record.get("model")),
                   harness=_load_choice(record.get("harness")),
                   session_id=session_id,
                   queued_at=_as_optional_float(record.get("queued_at")),
                   run_id=_as_optional_text(record.get("run_id")),
                   started_at=_as_optional_float(record.get("started_at")),
                   finished_at=_as_optional_float(record.get("finished_at")),
                   last_error=_as_optional_text(record.get("last_error")),
                   created_at=created_at, updated_at=updated_at)


def _copy(task: BoardTask) -> BoardTask:
    return replace(
        task,
        tags=list(task.tags),
        comments=[dict(comment) for comment in task.comments],
        runs=[dict(run) for run in task.runs],
    )


def _release_comments(task: BoardTask, run_id: str) -> None:
    """Make the comments consumed by ``run_id`` pending again.

    Used when a run is interrupted or the human moves its card out of
    Doing: the feedback never got a final answer, so the next run should
    send it instead of losing it.
    """
    for comment in task.comments:
        if comment.get("run_id") == run_id:
            comment["run_id"] = None


def _queue_key(task: BoardTask) -> tuple[int, int, float, str]:
    """Priority first, then the column order, then age."""
    return (-PRIORITY_WEIGHT[task.priority], task.position,
            task.created_at, task.id)


def _queue_candidates(tasks: list[BoardTask]) -> list[BoardTask]:
    return [task for task in tasks
            if task.status == "todo" and task.queued_at is not None
            and task.description.strip()]


def _normalize(tasks: list[BoardTask]) -> list[BoardTask]:
    """Group tasks by column and renumber each column from zero."""
    ordered = sorted(
        tasks,
        key=lambda task: (_STATUS_INDEX[task.status], task.position,
                          task.created_at, task.id),
    )
    counters = {status: 0 for status in BOARD_STATUSES}
    for task in ordered:
        task.position = counters[task.status]
        counters[task.status] += 1
    return ordered


class BoardStore:
    """Load, mutate, and persist the single web UI board."""

    def __init__(self, root: Path | None = None) -> None:
        # ``root`` is the runtime home; tests pass a temporary directory.
        # Without one, the shared storage helper decides the location.
        if root is None:
            self._path = storage.board_path()
        else:
            self._path = Path(root) / "board.json"
        self._tasks: list[BoardTask] = []
        self._updated_at = 0.0
        self._lock = RLock()

    @property
    def path(self) -> Path:
        return self._path

    def load(self) -> None:
        """Read the board from disk, tolerating a missing or corrupt file."""
        with self._lock:
            self._tasks = []
            self._updated_at = 0.0
            if not self._path.is_file():
                return
            try:
                raw = json.loads(self._path.read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                self._backup_corrupt(exc)
                return
            tasks_record = raw.get("tasks") if isinstance(raw, dict) else None
            if not isinstance(tasks_record, list):
                self._backup_corrupt("missing tasks list")
                return
            loaded: list[BoardTask] = []
            for record in tasks_record:
                task = BoardTask.from_record(record)
                if task is None:
                    logger.warning("webui.board.task_skipped path=%s",
                                   self._path)
                    continue
                loaded.append(task)
            self._tasks = _normalize(loaded)
            self._updated_at = _as_float(
                raw.get("updated_at") if isinstance(raw, dict) else None)

    def snapshot(self, sessions: "SessionStore") -> dict[str, Any]:
        """The complete board, ordered by column and position."""
        with self._lock:
            tasks = sorted(
                self._tasks,
                key=lambda task: (_STATUS_INDEX[task.status], task.position,
                                  task.created_at, task.id),
            )
            rendered: list[dict[str, Any]] = []
            for task in tasks:
                record = task.to_record()
                # The polled snapshot stays small: activity is fetched
                # on demand from the task detail endpoint.
                record["comment_count"] = len(record.pop("comments"))
                record["run_count"] = len(record.pop("runs"))
                record["session"] = self._session_view(task.session_id,
                                                       sessions)
                rendered.append(record)
            return {
                "version": BOARD_VERSION,
                "statuses": list(BOARD_STATUSES),
                "updated_at": self._updated_at,
                "tasks": rendered,
            }

    def list_all(self) -> list[dict[str, Any]]:
        with self._lock:
            return [task.to_record() for task in self._tasks]

    def get(self, task_id: str) -> BoardTask | None:
        with self._lock:
            task = self._find(task_id)
            return _copy(task) if task is not None else None

    def create(self, title: str, *, description: str,
               status: str = "backlog", priority: str = "normal",
               tags: list[str] | None = None,
               model: str | None = None,
               harness: str | None = None) -> dict[str, Any]:
        title = _clean_title(title)
        description = _clean_description(description)
        status = _clean_status(status)
        priority = _clean_priority(priority)
        tags = _clean_tags(tags)
        model = _clean_choice(model, "model")
        harness = _clean_choice(harness, "harness")
        now = time.time()
        with self._lock:
            position = sum(1 for task in self._tasks
                           if task.status == status)
            task = BoardTask(
                id=f"task-{uuid4().hex[:12]}",
                title=title,
                description=description,
                status=status,
                priority=priority,
                tags=tags,
                position=position,
                model=model,
                harness=harness,
                queued_at=now if status == "todo" else None,
                created_at=now,
                updated_at=now,
            )
            tasks = [_copy(current) for current in self._tasks] + [task]
            self._commit(tasks, now)
            created = self._find(task.id)
            assert created is not None
            logger.debug("webui.board.task_created id=%s status=%s",
                         created.id, created.status)
            return created.to_record()

    def update(self, task_id: str, patch: dict[str, Any]) -> dict[str, Any]:
        """Apply a partial update, including a move within or across columns."""
        now = time.time()
        with self._lock:
            current = self._find(task_id)
            if current is None:
                raise BoardNotFoundError("task not found")

            tasks = [_copy(task) for task in self._tasks]
            target = next(task for task in tasks if task.id == task_id)

            if "title" in patch:
                target.title = _clean_title(patch["title"])
            if "description" in patch:
                target.description = _clean_description(patch["description"])
            if "priority" in patch:
                target.priority = _clean_priority(patch["priority"])
            if "tags" in patch:
                target.tags = _clean_tags(patch["tags"])
            if "model" in patch:
                target.model = _clean_choice(patch["model"], "model")
            if "harness" in patch:
                target.harness = _clean_choice(patch["harness"], "harness")

            new_status = target.status
            if "status" in patch:
                new_status = _clean_status(patch["status"])
                target.status = new_status
                if new_status == "todo":
                    if not target.description.strip():
                        raise BoardValidationError(
                            "description must not be empty before queuing a task")
                    target.queued_at = now
                    target.last_error = None
                else:
                    target.queued_at = None
                    if new_status == "doing" and current.status != "doing":
                        # A human moving a card into Doing is not an
                        # executor run; clear the run metadata so a
                        # restart cannot mistake it for an interrupted one.
                        target.run_id = None
                        target.started_at = None
                        target.finished_at = None
                        target.last_error = None
                    if new_status == "done":
                        target.last_error = None
            elif target.status == "todo" and target.queued_at is None:
                # Old boards may contain Todo cards that predate the
                # executor. Editing one is an explicit intent to use it,
                # so queue it without disturbing cards already queued.
                target.queued_at = now
                target.last_error = None

            if (current.status == "doing" and new_status != "doing"
                    and current.run_id):
                # The human moved the card while the executor owned it.
                # The run is cancelled, so any comments it consumed are
                # still waiting for an answer.
                _release_comments(target, current.run_id)

            # Mirrors the HTTP contract: a supplied status means "move",
            # even when it names the current column, so an explicit move
            # to the bottom is distinguishable from an edit that leaves
            # the status alone.
            status_given = "status" in patch
            position_given = patch.get("position") is not None
            move_requested = status_given or position_given

            target.updated_at = now
            if move_requested:
                tasks = [task for task in tasks if task.id != task_id]
                column = [task for task in tasks if task.status == new_status]
                if position_given:
                    raw_position = patch["position"]
                    if (not isinstance(raw_position, int)
                            or isinstance(raw_position, bool)):
                        raise BoardValidationError(
                            "position must be a non-negative integer")
                    index = min(max(raw_position, 0), len(column))
                else:
                    index = len(column)
                column.insert(index, target)
                for column_index, task in enumerate(column):
                    task.position = column_index
                tasks = [task for task in tasks
                         if task.status != new_status] + column

            self._commit(tasks, now)
            updated = self._find(task_id)
            assert updated is not None
            logger.debug("webui.board.task_updated id=%s status=%s position=%s",
                         updated.id, updated.status, updated.position)
            return updated.to_record()

    def move(self, task_id: str, status: str,
             position: int | None = None) -> dict[str, Any]:
        patch: dict[str, Any] = {"status": status}
        if position is not None:
            patch["position"] = position
        return self.update(task_id, patch)

    def add_comment(self, task_id: str,
                    body: str) -> tuple[dict[str, Any], bool]:
        """Append a comment and queue the task for its next run.

        Returns the updated task and whether the comment has to wait for
        the current run to finish.  A comment never interrupts a turn:
        the executor-owned card stays in Doing, ``finish_run`` sees the
        pending comment and sends the card back to Todo instead of
        Review.
        """
        text = _clean_comment(body)
        now = time.time()
        with self._lock:
            if self._find(task_id) is None:
                raise BoardNotFoundError("task not found")
            tasks = [_copy(task) for task in self._tasks]
            target = next(task for task in tasks if task.id == task_id)
            target.comments.append({
                "id": f"comment-{uuid4().hex[:12]}",
                "body": text,
                "created_at": now,
                "run_id": None,
            })
            deferred = target.status == "doing" and bool(target.run_id)
            if not deferred:
                todo_count = sum(1 for task in tasks
                                 if task.id != task_id
                                 and task.status == "todo")
                target.status = "todo"
                target.queued_at = now
                target.run_id = None
                target.started_at = None
                target.finished_at = None
                target.last_error = None
                target.position = todo_count
            target.updated_at = now
            self._commit(tasks, now)
            updated = self._find(task_id)
            assert updated is not None
            logger.debug("webui.board.comment_added id=%s deferred=%s",
                         task_id, deferred)
            return updated.to_record(), deferred

    def detail(self, task_id: str,
               sessions: "SessionStore") -> dict[str, Any]:
        """One task with its full comment and run history."""
        with self._lock:
            task = self._find(task_id)
            if task is None:
                raise BoardNotFoundError("task not found")
            record = task.to_record()
            record["comment_count"] = len(record["comments"])
            record["run_count"] = len(record["runs"])
            record["session"] = self._session_view(task.session_id, sessions)
            return record

    def delete(self, task_id: str) -> bool:
        now = time.time()
        with self._lock:
            if self._find(task_id) is None:
                return False
            tasks = [_copy(task) for task in self._tasks
                     if task.id != task_id]
            self._commit(tasks, now)
            logger.debug("webui.board.task_deleted id=%s", task_id)
            return True

    def next_todo(self) -> BoardTask | None:
        """The next task the executor would claim, without changing it."""
        with self._lock:
            candidates = _queue_candidates(self._tasks)
            if not candidates:
                return None
            return _copy(min(candidates, key=_queue_key))

    def claim_next_todo(self) -> BoardTask | None:
        """Atomically move the highest-priority queued task to Doing."""
        now = time.time()
        with self._lock:
            candidates = _queue_candidates(self._tasks)
            if not candidates:
                return None
            chosen = min(candidates, key=_queue_key)
            tasks = [_copy(task) for task in self._tasks]
            target = next(task for task in tasks if task.id == chosen.id)
            doing_count = sum(1 for task in tasks
                              if task.id != chosen.id
                              and task.status == "doing")
            target.status = "doing"
            target.queued_at = None
            target.run_id = f"run-{uuid4().hex[:12]}"
            target.started_at = now
            target.finished_at = None
            target.last_error = None
            if target.runs:
                # The first run always sends the description verbatim.
                # Later runs carry the feedback that queued this card;
                # mark it as consumed by this run before it starts.
                for comment in target.comments:
                    if comment.get("run_id") is None:
                        comment["run_id"] = target.run_id
            target.position = doing_count
            target.updated_at = now
            self._commit(tasks, now)
            claimed = self._find(chosen.id)
            assert claimed is not None
            logger.debug("webui.board.task_claimed id=%s run=%s",
                         claimed.id, claimed.run_id)
            return _copy(claimed)

    def finish_run(self, task_id: str, run_id: str, *,
                   status: str = "review",
                   error: str | None = None,
                   response: str = "",
                   result: str = "completed") -> bool:
        """Finish one executor run, but only if it still owns the card.

        The run's final answer is appended to the task history.  If new
        comments arrived while it was running (or were already waiting
        before the first description run), the card returns to Todo
        instead of Review so the executor sends the feedback next.
        """
        now = time.time()
        if result not in RUN_STATUSES:
            raise BoardValidationError(
                "run result must be one of " + ", ".join(RUN_STATUSES))
        with self._lock:
            task = self._find(task_id)
            if task is None or task.status != "doing" or task.run_id != run_id:
                return False
            status = _clean_status(status)
            tasks = [_copy(current) for current in self._tasks]
            target = next(current for current in tasks
                          if current.id == task_id)
            target.runs.append({
                "run_id": run_id,
                "started_at": target.started_at,
                "finished_at": now,
                "status": result,
                "response": _clean_response(response),
                "error": _clean_error(error),
            })
            pending = any(comment.get("run_id") is None
                          for comment in target.comments)
            if pending:
                todo_count = sum(1 for current in tasks
                                 if current.id != task_id
                                 and current.status == "todo")
                target.status = "todo"
                target.queued_at = now
                target.run_id = None
                target.started_at = None
                target.finished_at = now
                target.last_error = _clean_error(error)
                target.position = todo_count
            else:
                column_count = sum(1 for current in tasks
                                   if current.id != task_id
                                   and current.status == status)
                target.status = status
                target.queued_at = None
                target.finished_at = now
                target.last_error = _clean_error(error)
                target.position = column_count
            target.updated_at = now
            self._commit(tasks, now)
            logger.debug("webui.board.task_finished id=%s status=%s "
                         "result=%s pending_comments=%s error=%s",
                         task_id, target.status, result, pending, bool(error))
            return True

    def requeue_run(self, task_id: str, run_id: str, *,
                    error: str | None = None) -> bool:
        """Put an interrupted run back at the end of Todo."""
        now = time.time()
        with self._lock:
            task = self._find(task_id)
            if task is None or task.status != "doing" or task.run_id != run_id:
                return False
            tasks = [_copy(current) for current in self._tasks]
            target = next(current for current in tasks
                          if current.id == task_id)
            column_count = sum(1 for current in tasks
                               if current.id != task_id
                               and current.status == "todo")
            target.status = "todo"
            target.queued_at = now
            target.run_id = None
            target.started_at = None
            target.finished_at = None
            target.last_error = _clean_error(error)
            target.position = column_count
            target.updated_at = now
            _release_comments(target, run_id)
            self._commit(tasks, now)
            logger.debug("webui.board.task_requeued id=%s", task_id)
            return True

    def recover_interrupted(self) -> int:
        """Return executor-owned Doing tasks to Todo after a restart."""
        now = time.time()
        with self._lock:
            tasks = [_copy(task) for task in self._tasks]
            changed = 0
            todo_count = sum(1 for task in tasks if task.status == "todo")
            for task in tasks:
                if task.status == "doing" and task.run_id:
                    task.status = "todo"
                    task.queued_at = now
                    _release_comments(task, task.run_id)
                    task.run_id = None
                    task.started_at = None
                    task.finished_at = None
                    task.last_error = "Interrupted by server restart"
                    task.position = todo_count
                    task.updated_at = now
                    todo_count += 1
                    changed += 1
            if changed:
                self._commit(tasks, now)
                logger.info("webui.board.recovered tasks=%d path=%s",
                            changed, self._path)
            return changed

    def ensure_session(self, task_id: str, sessions: "SessionStore", *,
                       model: str | None = None,
                       harness: str | None = None) -> tuple[str, bool]:
        """Return the task's conversation, creating and linking one if needed."""
        now = time.time()
        with self._lock:
            task = self._find(task_id)
            if task is None:
                raise BoardNotFoundError("task not found")
            if task.session_id:
                session = sessions.get(task.session_id)
                if session is not None:
                    return session.id, False

            session = sessions.create(title=task.title,
                                      model=task.model or model,
                                      harness=task.harness or harness)
            tasks = [_copy(current) for current in self._tasks]
            target = next(current for current in tasks
                          if current.id == task_id)
            target.session_id = session.id
            target.updated_at = now
            self._commit(tasks, now)
            logger.debug("webui.board.task_linked id=%s session=%s",
                         task_id, session.id)
            return session.id, True

    def unlink_session(self, session_id: str) -> None:
        """Clear a deleted conversation from any task that referenced it."""
        now = time.time()
        with self._lock:
            if not any(task.session_id == session_id for task in self._tasks):
                return
            tasks = [_copy(task) for task in self._tasks]
            for task in tasks:
                if task.session_id == session_id:
                    task.session_id = None
                    task.updated_at = now
            self._commit(tasks, now)
            logger.debug("webui.board.session_unlinked session=%s", session_id)

    def reconcile(self, sessions: "SessionStore") -> None:
        """Drop links whose conversation no longer exists."""
        now = time.time()
        with self._lock:
            tasks = [_copy(task) for task in self._tasks]
            changed = False
            for task in tasks:
                if task.session_id and sessions.get(task.session_id) is None:
                    task.session_id = None
                    task.updated_at = now
                    changed = True
            if not changed:
                return
            self._commit(tasks, now)
            logger.info("webui.board.reconciled path=%s", self._path)

    def _find(self, task_id: str) -> BoardTask | None:
        return next((task for task in self._tasks if task.id == task_id), None)

    def _commit(self, tasks: list[BoardTask], now: float) -> None:
        normalized = _normalize(tasks)
        record = {
            "version": BOARD_VERSION,
            "updated_at": now,
            "tasks": [task.to_record() for task in normalized],
        }
        self._write_atomic(record)
        self._tasks = normalized
        self._updated_at = now

    def _write_atomic(self, record: dict[str, Any]) -> None:
        temporary = self._path.with_name(self._path.name + ".tmp")
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            temporary.write_text(
                json.dumps(record, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
            os.replace(temporary, self._path)
        except (OSError, TypeError, ValueError) as exc:
            try:
                temporary.unlink()
            except OSError:
                pass
            raise BoardStorageError(
                f"could not write the board: {exc}") from exc

    def _backup_corrupt(self, reason: Any) -> None:
        backup = self._path.with_name(
            self._path.name + f".corrupt-{int(time.time())}")
        try:
            shutil.copy2(self._path, backup)
            logger.warning(
                "webui.board.corrupt path=%s backup=%s reason=%s",
                self._path, backup, reason)
        except OSError as exc:  # pragma: no cover - filesystem dependent
            logger.warning(
                "webui.board.corrupt_backup_failed path=%s error=%s",
                self._path, exc)

    @staticmethod
    def _session_view(session_id: str | None,
                      sessions: "SessionStore") -> dict[str, Any] | None:
        if not session_id:
            return None
        session = sessions.get(session_id)
        if session is None:
            return None
        return {
            "id": session.id,
            "title": session.title,
            "busy": bool(session.busy),
            "model": session.model,
            "harness": session.harness,
            "harness_locked": session.harness_locked,
            "updated_at": session.updated_at,
        }


__all__ = [
    "BOARD_PRIORITIES",
    "BOARD_STATUSES",
    "BOARD_VERSION",
    "PRIORITY_WEIGHT",
    "BoardError",
    "BoardNotFoundError",
    "BoardStorageError",
    "BoardStore",
    "BoardTask",
    "BoardValidationError",
]