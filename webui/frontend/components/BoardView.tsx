"use client";

import { Fragment, useState, type DragEvent } from "react";

import { STATUS_LABEL_KEYS, STATUS_ORDER, queueOrder } from "@/lib/board";
import { useI18n } from "@/lib/i18n";
import type { BoardSnapshot, BoardTask, TaskStatus } from "@/lib/types";

import { PlusIcon, RefreshIcon } from "./Icons";
import TaskCard from "./TaskCard";

interface BoardViewProps {
  snapshot: BoardSnapshot | null;
  loading: boolean;
  error: string | null;
  filter: string;
  hideDone: boolean;
  paused: boolean;
  onTogglePaused: () => void;
  onRefresh: () => void;
  onCreateTask: (status: TaskStatus) => void;
  onEditTask: (task: BoardTask) => void;
  onViewTask: (task: BoardTask) => void;
  onDeleteTask: (task: BoardTask) => void;
  onOpenTask: (task: BoardTask) => void;
  onMoveTask: (taskId: string, status: TaskStatus,
               position: number) => Promise<void> | void;
}

interface DropTarget {
  status: TaskStatus;
  index: number;
}

export default function BoardView({
  snapshot,
  loading,
  error,
  filter,
  hideDone,
  paused,
  onTogglePaused,
  onRefresh,
  onCreateTask,
  onEditTask,
  onViewTask,
  onDeleteTask,
  onOpenTask,
  onMoveTask,
}: BoardViewProps) {
  const { t } = useI18n();
  const [dragTaskId, setDragTaskId] = useState<string | null>(null);
  const [dropTarget, setDropTarget] = useState<DropTarget | null>(null);

  const statuses = snapshot?.statuses ?? STATUS_ORDER;
  const queuePositions = queueOrder(snapshot?.tasks ?? []);
  const needle = filter.trim().toLowerCase();
  const filtering = Boolean(needle) || hideDone;

  const matches = (task: BoardTask) => {
    if (hideDone && task.status === "done") return false;
    if (!needle) return true;
    return [task.title, task.description, ...task.tags].some(
      (value) => value.toLowerCase().indexOf(needle) !== -1,
    );
  };

  const columns = new Map<TaskStatus, BoardTask[]>();
  for (const status of statuses) columns.set(status, []);
  for (const task of snapshot?.tasks ?? []) {
    const bucket = columns.get(task.status);
    if (bucket) bucket.push(task);
  }

  const visibleCount = (snapshot?.tasks ?? []).filter(matches).length;

  const handleDragStart = (
    event: DragEvent<HTMLElement>,
    taskId: string,
  ) => {
    event.dataTransfer.effectAllowed = "move";
    event.dataTransfer.setData("text/plain", taskId);
    setDragTaskId(taskId);
  };

  const clearDrag = () => {
    setDragTaskId(null);
    setDropTarget(null);
  };

  const handleDragOver = (
    event: DragEvent<HTMLElement>,
    status: TaskStatus,
  ) => {
    event.preventDefault();
    event.dataTransfer.dropEffect = "move";
    const cards = Array.from(
      event.currentTarget.querySelectorAll<HTMLElement>("[data-task-id]"),
    );
    let index = cards.length;
    for (let position = 0; position < cards.length; position += 1) {
      const rect = cards[position].getBoundingClientRect();
      if (event.clientY < rect.top + rect.height / 2) {
        index = position;
        break;
      }
    }
    setDropTarget({ status, index });
  };

  const handleDrop = (
    event: DragEvent<HTMLElement>,
    status: TaskStatus,
  ) => {
    event.preventDefault();
    const taskId = event.dataTransfer.getData("text/plain") || dragTaskId;
    const currentTasks = columns.get(status) ?? [];
    const targetIndex = dropTarget?.status === status
      ? dropTarget.index
      : currentTasks.length;
    clearDrag();
    if (!taskId) return;

    // A text filter hides cards, so DOM positions no longer map onto
    // the persisted order; append instead of guessing.
    if (filtering) {
      void onMoveTask(taskId, status, Number.MAX_SAFE_INTEGER);
      return;
    }

    const currentIndex = currentTasks.findIndex((task) => task.id === taskId);
    let index = targetIndex;
    if (currentIndex !== -1 && currentIndex < index) index -= 1;
    if (currentIndex !== -1 && currentIndex === index) return;
    void onMoveTask(taskId, status, index);
  };

  return (
    <div className="board">
      <header className="board-head">
        <div className="board-head-left">
          <h1 className="board-heading">{t("boardTitle")}</h1>
          <span className="board-subtitle">
            {t("boardTaskCount", { count: visibleCount })}
          </span>
          {paused ? (
            <span className="board-paused-badge">
              {t("boardAutoRunPaused")}
            </span>
          ) : null}
        </div>
        <div className="board-head-actions">
          <button
            className="btn small"
            type="button"
            onClick={onTogglePaused}
          >
            {paused ? t("boardResumeAutoRun") : t("boardPauseAutoRun")}
          </button>
          <button
            className="icon-btn"
            type="button"
            title={t("boardRefresh")}
            aria-label={t("boardRefresh")}
            onClick={onRefresh}
          >
            <RefreshIcon />
          </button>
          <button
            className="btn primary board-new-btn"
            type="button"
            onClick={() => onCreateTask("backlog")}
          >
            <PlusIcon />
            {t("boardNewTask")}
          </button>
        </div>
      </header>

      {!snapshot ? (
        <div className="board-placeholder">
          {loading ? (
            <span>{t("boardLoading")}</span>
          ) : (
            <>
              <span>{error ?? t("boardLoadFailed")}</span>
              <button className="btn" type="button" onClick={onRefresh}>
                {t("boardRetry")}
              </button>
            </>
          )}
        </div>
      ) : (
        <div className="board-columns">
          {statuses.map((status) => {
            const columnTasks = (columns.get(status) ?? []).filter(matches);
            const columnDrop = dropTarget?.status === status
              ? dropTarget
              : null;
            const showDrop = columnDrop !== null;
            return (
              <section
                className={`board-column${showDrop ? " drag-over" : ""}`}
                key={status}
                onDragOver={(event) => handleDragOver(event, status)}
                onDrop={(event) => handleDrop(event, status)}
              >
                <header className="board-column-head">
                  <span className="board-column-title">
                    {t(STATUS_LABEL_KEYS[status])}
                  </span>
                  <span className="board-column-count">
                    {columnTasks.length}
                  </span>
                  <button
                    className="icon-btn small"
                    type="button"
                    title={t("boardNewTask")}
                    aria-label={t("boardNewTask")}
                    onClick={() => onCreateTask(status)}
                  >
                    <PlusIcon />
                  </button>
                </header>

                <div className="board-cards">
                  {columnTasks.map((task, index) => (
                    <Fragment key={task.id}>
                      {columnDrop && columnDrop.index === index ? (
                        <div className="board-drop-line" />
                      ) : null}
                      <TaskCard
                        task={task}
                        statuses={statuses}
                        queueOrder={queuePositions.get(task.id)}
                        dragging={dragTaskId === task.id}
                        onDragStart={handleDragStart}
                        onDragEnd={clearDrag}
                        onEdit={onEditTask}
                        onView={onViewTask}
                        onDelete={onDeleteTask}
                        onOpen={onOpenTask}
                        onMove={onMoveTask}
                      />
                    </Fragment>
                  ))}
                  {columnDrop && columnDrop.index >= columnTasks.length ? (
                    <div className="board-drop-line" />
                  ) : null}
                  {columnTasks.length === 0 && !showDrop ? (
                    <div className="board-empty-column">
                      {t("boardEmptyColumn")}
                    </div>
                  ) : null}
                </div>
              </section>
            );
          })}
        </div>
      )}
    </div>
  );
}