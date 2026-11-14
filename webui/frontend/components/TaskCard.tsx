"use client";

import type { DragEvent } from "react";

import {
  PRIORITY_LABEL_KEYS,
  STATUS_LABEL_KEYS,
  relativeTime,
} from "@/lib/board";
import { useI18n } from "@/lib/i18n";
import type { BoardTask, TaskStatus } from "@/lib/types";

import { ChatIcon, CloseIcon, EditIcon } from "./Icons";

interface TaskCardProps {
  task: BoardTask;
  statuses: TaskStatus[];
  queueOrder?: number;
  dragging: boolean;
  onDragStart: (event: DragEvent<HTMLElement>, taskId: string) => void;
  onDragEnd: () => void;
  onEdit: (task: BoardTask) => void;
  onDelete: (task: BoardTask) => void;
  onOpen: (task: BoardTask) => void;
  onMove: (taskId: string, status: TaskStatus, position: number) => void;
}

export default function TaskCard({
  task,
  statuses,
  queueOrder,
  dragging,
  onDragStart,
  onDragEnd,
  onEdit,
  onDelete,
  onOpen,
  onMove,
}: TaskCardProps) {
  const { t } = useI18n();
  const visibleTags = task.tags.slice(0, 3);
  const hiddenTags = task.tags.length - visibleTags.length;

  return (
    <article
      className={`task-card${dragging ? " dragging" : ""}`}
      data-task-id={task.id}
      draggable
      role="button"
      tabIndex={0}
      onClick={() => onEdit(task)}
      onKeyDown={(event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          onEdit(task);
        }
      }}
      onDragStart={(event) => onDragStart(event, task.id)}
      onDragEnd={onDragEnd}
    >
      <div className="task-card-top">
        <span className="task-badges">
          {queueOrder ? (
            <span
              className="task-queue"
              title={`${t("boardQueue")} ${queueOrder}`}
            >
              #{queueOrder}
            </span>
          ) : null}
          <span className={`task-priority task-priority-${task.priority}`}>
            {t(PRIORITY_LABEL_KEYS[task.priority])}
          </span>
        </span>
        <div
          className="task-actions"
          onClick={(event) => event.stopPropagation()}
          onKeyDown={(event) => event.stopPropagation()}
        >
          <button
            className="icon-btn small"
            type="button"
            title={t("boardOpenConversation")}
            aria-label={t("boardOpenConversation")}
            onClick={() => onOpen(task)}
          >
            <ChatIcon />
          </button>
          <button
            className="icon-btn small"
            type="button"
            title={t("boardEditTask")}
            aria-label={t("boardEditTask")}
            onClick={() => onEdit(task)}
          >
            <EditIcon />
          </button>
          <button
            className="icon-btn small"
            type="button"
            title={t("boardDeleteTask")}
            aria-label={t("boardDeleteTask")}
            onClick={() => onDelete(task)}
          >
            <CloseIcon size={13} />
          </button>
        </div>
      </div>

      <div className="task-title">{task.title}</div>
      {task.description ? (
        <p className="task-description">{task.description}</p>
      ) : null}

      {task.last_error ? (
        <p className="task-error" title={task.last_error}>
          <span className="task-error-label">{t("boardRunFailed")}</span>
          {task.last_error}
        </p>
      ) : null}

      {task.tags.length ? (
        <div className="task-tags">
          {visibleTags.map((tag) => (
            <span className="task-tag" key={tag}>{tag}</span>
          ))}
          {hiddenTags > 0 ? (
            <span className="task-tag">+{hiddenTags}</span>
          ) : null}
        </div>
      ) : null}

      <div className="task-foot">
        {task.session ? (
          <span className={`task-session${task.session.busy ? " busy" : ""}`}>
            <ChatIcon />
            {task.session.busy
              ? t("boardRunning")
              : relativeTime(task.session.updated_at, t)}
          </span>
        ) : (
          <span className="task-session muted">{t("boardUnlinked")}</span>
        )}

        <select
          className="task-move"
          aria-label={t("boardMoveTo")}
          title={t("boardMoveTo")}
          value={task.status}
          draggable={false}
          onClick={(event) => event.stopPropagation()}
          onKeyDown={(event) => event.stopPropagation()}
          onPointerDown={(event) => event.stopPropagation()}
          onDragStart={(event) => event.stopPropagation()}
          onChange={(event) => {
            const status = event.target.value as TaskStatus;
            if (status !== task.status) {
              onMove(task.id, status, Number.MAX_SAFE_INTEGER);
            }
          }}
        >
          {statuses.map((status) => (
            <option value={status} key={status}>
              {t(STATUS_LABEL_KEYS[status])}
            </option>
          ))}
        </select>
      </div>
    </article>
  );
}