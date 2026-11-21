"use client";

import { useI18n } from "@/lib/i18n";
import { formatDayTime } from "@/lib/evaluation";
import type { EvalDiagnosisRow } from "@/lib/types";

interface DiagnosisPanelProps {
  rows: EvalDiagnosisRow[];
  onOpenConversation: (sessionId: string) => void;
}

/**
 * History of diagnosis conversations.
 *
 * A diagnosis is a normal session (``origin=diagnosis``) linked to its
 * target through ``Session.target``; the report is the segment pointed at
 * by ``report_segment_id``.  Starting one is a manual action.
 */
export default function DiagnosisPanel({
  rows,
  onOpenConversation,
}: DiagnosisPanelProps) {
  const { t } = useI18n();
  if (rows.length === 0) {
    return (
      <div className="eval-empty">
        <p>{t("evalNoDiagnoses")}</p>
        <p className="eval-empty-hint">{t("evalDiagnosisHint")}</p>
      </div>
    );
  }
  return (
    <ul className="eval-diagnoses">
      {rows.map((row) => (
        <li key={row.session_id} className="eval-diagnosis-item">
          <span className="eval-diagnosis-title">
            {row.title || row.session_id}
          </span>
          <span className="eval-diagnosis-meta">
            {row.origin}
            {row.target_type ? ` · ${row.target_type}` : ""}
            {row.target_id ? `: ${row.target_id}` : ""} ·{" "}
            {formatDayTime(row.updated_at)}
          </span>
          <span className={`eval-badge eval-badge-${row.report_segment_id
            ? "present" : "missing"}`}>
            {row.report_segment_id ? t("evalReportReady") : t("evalReportPending")}
          </span>
          <button
            type="button"
            className="eval-link"
            onClick={() => onOpenConversation(row.session_id)}
          >
            {t("evalOpenSession")}
          </button>
        </li>
      ))}
    </ul>
  );
}
