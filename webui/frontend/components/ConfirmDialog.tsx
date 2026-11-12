"use client";

import type { ReactNode } from "react";

import Modal from "./Modal";

interface ConfirmDialogProps {
  open: boolean;
  title: string;
  message: ReactNode;
  confirmText?: string;
  cancelText?: string;
  busyText?: string;
  tone?: "default" | "danger";
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

/** Reusable confirmation dialog; only the copy changes per use case. */
export default function ConfirmDialog({
  open,
  title,
  message,
  confirmText = "确定",
  cancelText = "取消",
  busyText = "处理中…",
  tone = "default",
  busy = false,
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  return (
    <Modal
      open={open}
      title={title}
      onClose={busy ? () => {} : onCancel}
      footer={
        <>
          <button
            className="btn"
            type="button"
            data-autofocus
            disabled={busy}
            onClick={onCancel}
          >
            {cancelText}
          </button>
          <button
            className={`btn${tone === "danger" ? " danger" : " primary"}`}
            type="button"
            disabled={busy}
            onClick={onConfirm}
          >
            {busy ? busyText : confirmText}
          </button>
        </>
      }
    >
      <p className="confirm-message">{message}</p>
    </Modal>
  );
}
