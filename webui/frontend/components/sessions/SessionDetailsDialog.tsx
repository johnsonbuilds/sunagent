"use client";

import type { SessionSummary } from "@/lib/types";
import { useI18n, type TFunction } from "@/lib/i18n";
import { useToast } from "@/lib/stores/toast";

import Modal from "../Modal";

interface DetailField {
  label: string;
  value: string;
  mono?: boolean;
}

function detailFields(session: SessionSummary, t: TFunction): DetailField[] {
  const missing = t("notSet");
  return [
    { label: t("detailId"), value: session.id, mono: true },
    { label: t("detailTitle"), value: session.title || t("untitled") },
    { label: t("detailModel"), value: session.model || missing, mono: true },
    {
      label: t("detailHarness"),
      value: session.harness || missing,
      mono: true,
    },
    { label: t("detailOrigin"), value: session.origin || missing },
  ];
}

interface SessionDetailsDialogProps {
  session: SessionSummary | null;
  onClose: () => void;
}

export default function SessionDetailsDialog({
  session,
  onClose,
}: SessionDetailsDialogProps) {
  const { t } = useI18n();
  const { showToast } = useToast();
  const rows = session ? detailFields(session, t) : [];

  // Copy exactly what the dialog shows, one "Label: value" line per field.
  const copy = async () => {
    const text = rows
      .map((row) => `${row.label}: ${row.value}`)
      .join("\n");
    try {
      await navigator.clipboard.writeText(text);
      showToast(t("msgCopied"), "success");
    } catch {
      showToast(t("copyFailed"), "error");
    }
  };

  return (
    <Modal
      open={session !== null}
      title={t("sessionDetails")}
      onClose={onClose}
      footer={
        <>
          <button className="btn" type="button" onClick={onClose}>
            {t("close")}
          </button>
          <button
            className="btn primary"
            type="button"
            data-autofocus
            onClick={() => void copy()}
          >
            {t("copy")}
          </button>
        </>
      }
    >
      <dl className="detail-list">
        {rows.map((row) => (
          <div className="detail-row" key={row.label}>
            <dt>{row.label}</dt>
            <dd className={row.mono ? "detail-mono" : undefined}>
              {row.value}
            </dd>
          </div>
        ))}
      </dl>
    </Modal>
  );
}
