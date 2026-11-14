"use client";

import { useEffect, useId, useRef, useState } from "react";

import {
  PRIORITY_LABEL_KEYS,
  PRIORITY_ORDER,
  STATUS_LABEL_KEYS,
} from "@/lib/board";
import { useI18n } from "@/lib/i18n";
import type { BoardTask, TaskInput, TaskPriority, TaskStatus } from "@/lib/types";

import Modal from "./Modal";

interface TaskEditorProps {
  open: boolean;
  task: BoardTask | null;
  initialStatus: TaskStatus;
  statuses: TaskStatus[];
  onClose: () => void;
  onSave: (input: TaskInput) => Promise<void>;
  onRequestDelete: (task: BoardTask) => void;
  onOpenConversation: (task: BoardTask) => void;
}

export default function TaskEditor({
  open,
  task,
  initialStatus,
  statuses,
  onClose,
  onSave,
  onRequestDelete,
  onOpenConversation,
}: TaskEditorProps) {
  const { t } = useI18n();
  const titleFieldId = useId();
  const descriptionFieldId = useId();
  const statusFieldId = useId();
  const priorityFieldId = useId();
  const tagsFieldId = useId();

  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [status, setStatus] = useState<TaskStatus>(initialStatus);
  const [priority, setPriority] = useState<TaskPriority>("normal");
  const [tagsText, setTagsText] = useState("");
  const [titleError, setTitleError] = useState("");
  const [descriptionError, setDescriptionError] = useState("");
  const [saving, setSaving] = useState(false);
  const lastResetKey = useRef("");

  const resetKey = open
    ? `${task?.id ?? "new"}:${task ? "" : initialStatus}`
    : "";

  useEffect(() => {
    if (!open) {
      lastResetKey.current = "";
      return;
    }
    // Polling replaces the task object every few seconds. Reset only
    // when a different card (or a fresh "new task") opens.
    if (lastResetKey.current === resetKey) return;
    lastResetKey.current = resetKey;
    setTitle(task?.title ?? "");
    setDescription(task?.description ?? "");
    setStatus(task?.status ?? initialStatus);
    setPriority(task?.priority ?? "normal");
    setTagsText((task?.tags ?? []).join(", "));
    setTitleError("");
    setDescriptionError("");
    setSaving(false);
  }, [open, resetKey, task, initialStatus]);

  const handleSave = async () => {
    const trimmedTitle = title.trim();
    const trimmedDescription = description.trim();
    const titleInvalid = !trimmedTitle;
    const descriptionInvalid = !trimmedDescription;
    setTitleError(titleInvalid ? t("boardTitleRequired") : "");
    setDescriptionError(
      descriptionInvalid ? t("boardDescriptionRequired") : "",
    );
    if (titleInvalid || descriptionInvalid) return;

    const tags = Array.from(new Set(
      tagsText.split(",").map((tag) => tag.trim()).filter(Boolean),
    ));
    setSaving(true);
    try {
      await onSave({
        title: trimmedTitle,
        description: trimmedDescription,
        status,
        priority,
        tags,
      });
    } catch {
      // The parent already reported the failure; keep the dialog open.
    } finally {
      setSaving(false);
    }
  };

  return (
    <Modal
      open={open}
      title={task ? t("boardEditTask") : t("boardNewTask")}
      onClose={saving ? () => {} : onClose}
      footer={
        <>
          {task ? (
            <button
              className="btn danger"
              type="button"
              disabled={saving}
              onClick={() => onRequestDelete(task)}
            >
              {t("boardDeleteTask")}
            </button>
          ) : null}
          <span className="modal-foot-spacer" />
          <button
            className="btn"
            type="button"
            disabled={saving}
            onClick={onClose}
          >
            {t("defaultCancel")}
          </button>
          <button
            className="btn primary"
            type="button"
            disabled={saving}
            onClick={() => void handleSave()}
          >
            {saving ? t("defaultBusy") : t("boardSave")}
          </button>
        </>
      }
    >
      <div className="field">
        <label className="field-label" htmlFor={titleFieldId}>
          {t("boardTitleField")}
          <span className="field-required" aria-hidden="true">*</span>
        </label>
        <input
          className="text-input"
          id={titleFieldId}
          data-autofocus
          value={title}
          maxLength={200}
          aria-required="true"
          aria-invalid={titleError ? true : undefined}
          onChange={(event) => {
            setTitle(event.target.value);
            if (titleError) setTitleError("");
          }}
        />
        {titleError ? <span className="field-error">{titleError}</span> : null}
      </div>

      <div className="field">
        <label className="field-label" htmlFor={descriptionFieldId}>
          {t("boardDescription")}
          <span className="field-required" aria-hidden="true">*</span>
        </label>
        <textarea
          className="text-input text-area"
          id={descriptionFieldId}
          rows={5}
          maxLength={20000}
          value={description}
          aria-required="true"
          aria-invalid={descriptionError ? true : undefined}
          onChange={(event) => {
            setDescription(event.target.value);
            if (descriptionError) setDescriptionError("");
          }}
        />
        {descriptionError ? (
          <span className="field-error">{descriptionError}</span>
        ) : null}
      </div>

      <div className="field-row">
        <div className="field">
          <label className="field-label" htmlFor={statusFieldId}>
            {t("boardStatus")}
          </label>
          <select
            className="text-input"
            id={statusFieldId}
            value={status}
            onChange={(event) => setStatus(event.target.value as TaskStatus)}
          >
            {statuses.map((item) => (
              <option value={item} key={item}>
                {t(STATUS_LABEL_KEYS[item])}
              </option>
            ))}
          </select>
        </div>

        <div className="field">
          <label className="field-label" htmlFor={priorityFieldId}>
            {t("boardPriority")}
          </label>
          <select
            className="text-input"
            id={priorityFieldId}
            value={priority}
            onChange={(event) => setPriority(
              event.target.value as TaskPriority,
            )}
          >
            {PRIORITY_ORDER.map((item) => (
              <option value={item} key={item}>
                {t(PRIORITY_LABEL_KEYS[item])}
              </option>
            ))}
          </select>
        </div>
      </div>

      <div className="field">
        <label className="field-label" htmlFor={tagsFieldId}>
          {t("boardTags")}
        </label>
        <input
          className="text-input"
          id={tagsFieldId}
          value={tagsText}
          placeholder={t("boardTagsPlaceholder")}
          onChange={(event) => setTagsText(event.target.value)}
        />
      </div>

      {task ? (
        <div className="field">
          <span className="field-label">{t("boardConversation")}</span>
          <div className="field-inline">
            <span className={`field-value${task.session ? "" : " muted"}`}>
              {task.session
                ? (task.session.busy
                  ? `${task.session.title} · ${t("boardRunning")}`
                  : task.session.title)
                : t("boardUnlinked")}
            </span>
            <button
              className="btn small"
              type="button"
              disabled={saving}
              onClick={() => onOpenConversation(task)}
            >
              {task.session
                ? t("boardOpenConversation")
                : t("boardCreateConversation")}
            </button>
          </div>
        </div>
      ) : null}
    </Modal>
  );
}