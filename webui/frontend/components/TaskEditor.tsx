"use client";

import { useCallback, useEffect, useId, useMemo, useRef, useState } from "react";

import {
  PRIORITY_LABEL_KEYS,
  PRIORITY_ORDER,
  STATUS_LABEL_KEYS,
} from "@/lib/board";
import { useI18n } from "@/lib/i18n";
import { useToast } from "@/lib/stores/toast";
import type {
  BoardTask,
  HarnessOption,
  ModelOption,
  TaskInput,
  TaskPriority,
  TaskStatus,
} from "@/lib/types";
import { useLeaveGuard } from "@/lib/useLeaveGuard";

import Modal from "./Modal";

function parseTags(text: string): string[] {
  return Array.from(
    new Set(text.split(",").map((tag) => tag.trim()).filter(Boolean)),
  );
}

const DEFAULT_TAG_CHIPS = ["bugfix", "feature"];

interface TaskEditorProps {
  open: boolean;
  task: BoardTask | null;
  initialStatus: TaskStatus;
  statuses: TaskStatus[];
  models: ModelOption[];
  harnesses: HarnessOption[];
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
  models,
  harnesses,
  onClose,
  onSave,
  onRequestDelete,
  onOpenConversation,
}: TaskEditorProps) {
  const { t } = useI18n();
  const { showToast, hideToast } = useToast();
  const titleFieldId = useId();
  const descriptionFieldId = useId();
  const statusFieldId = useId();
  const priorityFieldId = useId();
  const modelFieldId = useId();
  const harnessFieldId = useId();
  const tagsFieldId = useId();

  const [title, setTitle] = useState("");
  const [description, setDescription] = useState("");
  const [status, setStatus] = useState<TaskStatus>(initialStatus);
  const [priority, setPriority] = useState<TaskPriority>("normal");
  const [model, setModel] = useState("");
  const [harness, setHarness] = useState("");
  const [tagsText, setTagsText] = useState("");
  const [titleError, setTitleError] = useState("");

  const toggleTag = (tag: string) => {
    const current = parseTags(tagsText);
    const next = current.includes(tag)
      ? current.filter((item) => item !== tag)
      : [...current, tag];
    setTagsText(next.join(", "));
  };

  const hasTag = (tag: string) => parseTags(tagsText).includes(tag);
  const [descriptionError, setDescriptionError] = useState("");
  const [saving, setSaving] = useState(false);
  // Second close attempt discards the edits: the first one only warns.
  const [discardArmed, setDiscardArmed] = useState(false);
  const lastResetKey = useRef("");

  const resetKey = open
    ? `${task?.id ?? "new"}:${task ? "" : initialStatus}`
    : "";

  useEffect(() => {
    if (!open) {
      lastResetKey.current = "";
      setDiscardArmed(false);
      return;
    }
    if (lastResetKey.current === resetKey) return;
    lastResetKey.current = resetKey;
    setDiscardArmed(false);
    setTitle(task?.title ?? "");
    setDescription(task?.description ?? "");
    setStatus(task?.status ?? initialStatus);
    setPriority(task?.priority ?? "normal");
    setModel(task?.model ?? "");
    setHarness(task?.harness ?? "");
    setTagsText((task?.tags ?? []).join(", "));
    setTitleError("");
    setDescriptionError("");
    setSaving(false);
  }, [open, resetKey, task, initialStatus]);

  const initial = useMemo(() => ({
    title: task?.title ?? "",
    description: task?.description ?? "",
    status: task?.status ?? initialStatus,
    priority: task?.priority ?? "normal",
    model: task?.model ?? "",
    harness: task?.harness ?? "",
    tags: (task?.tags ?? []).join(", "),
  }), [task, initialStatus]);

  const dirty = open && (
    title !== initial.title
    || description !== initial.description
    || status !== initial.status
    || priority !== initial.priority
    || model !== initial.model
    || harness !== initial.harness
    || parseTags(tagsText).join(",") !== parseTags(initial.tags).join(",")
  );

  // Any edit after the warning disarms it again, so a stale confirmation can
  // never swallow work typed in between.
  useEffect(() => {
    setDiscardArmed(false);
  }, [title, description, status, priority, model, harness, tagsText]);

  // Leaving with unsaved edits is refused everywhere: the notice explains it
  // and the editor stays open, so nothing is ever dropped on a stray click.
  // Showing the notice also arms the discard, so the next close attempt
  // (whatever routed the user here) goes through.
  const notifyUnsaved = useCallback(() => {
    setDiscardArmed(true);
    showToast(t("boardUnsavedToast"), "info");
  }, [showToast, t]);

  useLeaveGuard(dirty, notifyUnsaved);

  const harnessLocked = task?.session?.harness_locked ?? false;
  const modelMissing = model !== "" && !models.some((item) => item.id === model);
  const harnessMissing = harness !== ""
    && !harnesses.some((item) => item.id === harness);

  const handleSave = async (): Promise<void> => {
    const trimmedTitle = title.trim();
    const trimmedDescription = description.trim();
    const titleInvalid = !trimmedTitle;
    const descriptionInvalid = !trimmedDescription;
    setTitleError(titleInvalid ? t("boardTitleRequired") : "");
    setDescriptionError(
      descriptionInvalid ? t("boardDescriptionRequired") : "",
    );
    if (titleInvalid || descriptionInvalid) return;

    const tags = parseTags(tagsText);
    setSaving(true);
    try {
      await onSave({
        title: trimmedTitle,
        description: trimmedDescription,
        status,
        priority,
        tags,
        model,
        harness,
      });
    } catch {
      // The store already surfaced the failure as an error toast.
    } finally {
      setSaving(false);
    }
  };

  const requestClose = () => {
    if (saving) return;
    if (dirty && !discardArmed) {
      notifyUnsaved();
      return;
    }
    if (dirty) hideToast();
    onClose();
  };

  const openConversation = () => {
    if (!task) return;
    if (dirty) {
      notifyUnsaved();
      return;
    }
    onOpenConversation(task);
  };

  return (
    <>
      <Modal
        open={open}
        title={task ? t("boardEditTask") : t("boardNewTask")}
        onClose={requestClose}
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
              onClick={requestClose}
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

        <div className="field-row">
          <div className="field">
            <label className="field-label" htmlFor={modelFieldId}>
              {t("boardModel")}
            </label>
            <select
              className="text-input"
              id={modelFieldId}
              value={model}
              onChange={(event) => setModel(event.target.value)}
            >
              <option value="">{t("boardDefaultModel")}</option>
              {modelMissing ? (
                <option value={model}>{model}</option>
              ) : null}
              {models.map((item) => (
                <option value={item.id} key={item.id}>
                  {item.label}
                </option>
              ))}
            </select>
          </div>

          <div className="field">
            <label className="field-label" htmlFor={harnessFieldId}>
              {t("boardHarness")}
            </label>
            <select
              className="text-input"
              id={harnessFieldId}
              value={harness}
              disabled={harnessLocked}
              title={harnessLocked ? t("harnessLockedHint") : undefined}
              onChange={(event) => setHarness(event.target.value)}
            >
              <option value="">{t("boardDefaultHarness")}</option>
              {harnessMissing ? (
                <option value={harness}>{harness}</option>
              ) : null}
              {harnesses.map((item) => (
                <option value={item.id} key={item.id}>
                  {item.id}
                </option>
              ))}
            </select>
            {harnessLocked ? (
              <span className="field-hint">{t("harnessLockedHint")}</span>
            ) : null}
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
          <div className="tag-chips">
            {DEFAULT_TAG_CHIPS.map((tag) => (
              <button
                type="button"
                key={tag}
                className={`tag-chip${hasTag(tag) ? " tag-chip-active" : ""}`}
                onClick={() => toggleTag(tag)}
              >
                {tag}
              </button>
            ))}
          </div>
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
                onClick={openConversation}
              >
                {task.session
                  ? t("boardOpenConversation")
                  : t("boardCreateConversation")}
              </button>
            </div>
          </div>
        ) : null}
      </Modal>
    </>
  );
}
