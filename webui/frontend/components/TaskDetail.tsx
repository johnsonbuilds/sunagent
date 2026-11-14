"use client";

import { useEffect, useRef, useState } from "react";

import { PRIORITY_LABEL_KEYS, STATUS_LABEL_KEYS, relativeTime } from "@/lib/board";
import { useI18n, type TFunction } from "@/lib/i18n";
import type {
  BoardTask,
  CommentResult,
  TaskComment,
  TaskDetail as TaskDetailData,
  TaskRun,
} from "@/lib/types";

import Modal from "./Modal";

interface TaskDetailProps {
  open: boolean;
  task: BoardTask | null;
  onClose: () => void;
  onEdit: (task: BoardTask) => void;
  onOpenConversation: (task: BoardTask) => void;
  onFetch: (taskId: string) => Promise<TaskDetailData>;
  onComment: (taskId: string, body: string) => Promise<CommentResult>;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function statusLabel(run: TaskRun, t: TFunction): string {
  if (run.status === "completed") return "";
  if (run.status === "cancelled") return t("boardRunCancelled");
  return t("boardRunFailed");
}

export default function TaskDetail({
  open,
  task,
  onClose,
  onEdit,
  onOpenConversation,
  onFetch,
  onComment,
}: TaskDetailProps) {
  const { t } = useI18n();
  const [detail, setDetail] = useState<TaskDetailData | null>(null);
  const [loading, setLoading] = useState(false);
  const [failure, setFailure] = useState("");
  const [body, setBody] = useState("");
  const [sending, setSending] = useState(false);
  const requestRef = useRef(0);

  useEffect(() => {
    if (!open || !task) {
      setDetail(null);
      setFailure("");
      setBody("");
      return;
    }
    const request = requestRef.current + 1;
    requestRef.current = request;
    setLoading(true);
    onFetch(task.id)
      .then((next) => {
        if (requestRef.current !== request) return;
        setDetail(next);
        setFailure("");
      })
      .catch((error) => {
        if (requestRef.current !== request) return;
        setFailure(errorMessage(error));
      })
      .finally(() => {
        if (requestRef.current === request) setLoading(false);
      });
  }, [open, task?.id, task?.updated_at, onFetch]);

  if (!task) return null;

  const comments = detail?.comments ?? [];
  const runs = detail?.runs ?? [];
  const knownRuns = new Set(runs.map((run) => run.run_id));
  const pending = comments.filter(
    (comment) => !comment.run_id || !knownRuns.has(comment.run_id),
  );
  const groups = runs.map((run) => ({
    run,
    comments: comments.filter((comment) => comment.run_id === run.run_id),
  }));
  const running = task.status === "doing";

  const submit = async () => {
    const text = body.trim();
    if (!text || sending) return;
    setSending(true);
    try {
      const result = await onComment(task.id, text);
      setDetail(result.detail);
      setFailure("");
      setBody("");
    } catch (error) {
      setFailure(errorMessage(error));
    } finally {
      setSending(false);
    }
  };

  const renderComment = (comment: TaskComment) => (
    <div className="activity-comment" key={comment.id}>
      <div className="activity-role">{t("boardComments")}</div>
      <div className="activity-body">{comment.body}</div>
      <div className="activity-meta">
        {relativeTime(comment.created_at, t)}
      </div>
    </div>
  );

  const renderRun = (run: TaskRun, index: number,
                     matched: TaskComment[]) => (
    <div className="activity-run" key={run.run_id}>
      {matched.length ? (
        matched.map(renderComment)
      ) : (
        <div className="activity-label">
          {index === 0 ? t("boardInitialRun") : t("boardRerun")}
        </div>
      )}
      <div className="activity-response-block">
        <div className="activity-role">{t("boardFinalResponse")}</div>
        <div className="activity-response">
          {run.response || t("boardNoFinalResponse")}
        </div>
        <div className="activity-meta">
          {run.finished_at
            ? relativeTime(run.finished_at, t)
            : null}
          {run.status !== "completed" ? (
            <span className="activity-status">
              {statusLabel(run, t)}
              {run.error ? ` ${run.error}` : ""}
            </span>
          ) : null}
        </div>
      </div>
    </div>
  );

  return (
    <Modal
      open={open}
      wide
      title={task.title}
      onClose={onClose}
      footer={
        <>
          <button className="btn" type="button" onClick={onClose}>
            {t("close")}
          </button>
          <button
            className="btn"
            type="button"
            onClick={() => onOpenConversation(task)}
          >
            {t("boardOpenConversation")}
          </button>
          <button
            className="btn primary"
            type="button"
            onClick={() => onEdit(task)}
          >
            {t("boardEditTask")}
          </button>
        </>
      }
    >
      <div className="task-detail">
        <div className="task-detail-meta">
          <span className={`task-priority task-priority-${task.priority}`}>
            {t(PRIORITY_LABEL_KEYS[task.priority])}
          </span>
          <span>{t(STATUS_LABEL_KEYS[task.status])}</span>
          {task.session ? (
            <span>{task.session.busy ? t("boardRunning") : t("boardConversation")}</span>
          ) : null}
        </div>

        <p className="task-detail-description">{task.description}</p>

        <section className="activity">
          <h3 className="activity-heading">{t("boardActivity")}</h3>
          {loading && !detail ? (
            <p className="activity-empty">{t("boardLoadingTask")}</p>
          ) : null}
          {!loading && groups.length === 0 && pending.length === 0 ? (
            <p className="activity-empty">{t("boardNoActivity")}</p>
          ) : null}
          {groups.map((group, index) => renderRun(group.run, index,
                                                  group.comments))}
          {pending.length ? (
            <div className="activity-run pending">
              <div className="activity-label">{t("boardCommentPending")}</div>
              {pending.map(renderComment)}
            </div>
          ) : null}
        </section>

        {failure ? <p className="field-error">{failure}</p> : null}

        <div className="task-comment">
          <textarea
            className="text-input text-area task-comment-input"
            value={body}
            placeholder={t("boardCommentPlaceholder")}
            aria-label={t("boardCommentPlaceholder")}
            disabled={sending}
            onChange={(event) => setBody(event.target.value)}
            onKeyDown={(event) => {
              if ((event.metaKey || event.ctrlKey) && event.key === "Enter") {
                event.preventDefault();
                void submit();
              }
            }}
          />
          <div className="task-comment-foot">
            <span className="field-hint">
              {running
                ? t("boardCommentDuringRunHint")
                : t("boardCommentHint")}
            </span>
            <button
              className="btn primary"
              type="button"
              disabled={!body.trim() || sending}
              onClick={() => void submit()}
            >
              {t("boardCommentSend")}
            </button>
          </div>
        </div>
      </div>
    </Modal>
  );
}
