"use client";

import { useI18n } from "@/lib/i18n";
import { formatDayTime } from "@/lib/evaluation";
import type { EvalDiagnosisReport, EvalDiagnosisRow, EvalDiagnosisTarget } from "@/lib/types";
import MarkdownAnswer from "@/components/evaluation/MarkdownAnswer";

interface DiagnosisPanelProps {
  rows: EvalDiagnosisRow[];
  busy: boolean;
  cohortFilters: Record<string, string>;
  onStart: (target: EvalDiagnosisTarget, force?: boolean) => void;
  onOpenConversation: (sessionId: string) => void;
}

function targetOf(row: EvalDiagnosisRow): EvalDiagnosisTarget | null {
  const target = row.target;
  if (!target || typeof target !== "object") return null;
  const type = (target as { type?: unknown }).type;
  if (type === "task" || type === "run") {
    const id = (target as { id?: unknown }).id;
    if (typeof id === "string" && id) return { type, id };
    return null;
  }
  if (type === "cohort") {
    const filters = (target as { filters?: unknown }).filters;
    return {
      type: "cohort",
      filters: (filters && typeof filters === "object"
        ? filters as Record<string, string>
        : {}),
    };
  }
  return null;
}

function Report({ report }: { report: EvalDiagnosisReport | undefined }) {
  const { t } = useI18n();
  if (!report?.available) {
    return <div className="eval-chart-empty">{t("evalReportPending")}</div>;
  }
  // ``MarkdownAnswer`` renders the suggestions section from the extracted
  // items; rendering ``SuggestionList`` again here would show it twice.
  return <MarkdownAnswer report={report} />;
}

/**
 * Diagnosis history plus the manual trigger.
 *
 * Nothing here runs on load: the buttons are the only way a diagnosis
 * starts (docs/harness-evaluation.md §5.1).  Each row shows the report
 * frozen on ``report_segment_id``; follow-up questions live in the session.
 */
export default function DiagnosisPanel({
  rows,
  busy,
  cohortFilters,
  onStart,
  onOpenConversation,
}: DiagnosisPanelProps) {
  const { t } = useI18n();
  const cohort: EvalDiagnosisTarget = { type: "cohort", filters: cohortFilters };
  return (
    <div className="eval-diagnosis-panel">
      <div className="eval-diagnosis-actions">
        <button
          type="button"
          className="eval-action"
          disabled={busy}
          onClick={() => onStart(cohort)}
        >
          {busy ? t("evalDiagnosisBusy") : t("evalDiagnosisStartCohort")}
        </button>
        <span className="eval-diagnosis-meta">{t("evalDiagnosisHint")}</span>
      </div>
      {rows.length === 0 ? (
        <div className="eval-empty">
          <p>{t("evalNoDiagnoses")}</p>
        </div>
      ) : (
        <ul className="eval-diagnoses">
          {rows.map((row) => {
            const target = targetOf(row);
            return (
              <li key={row.session_id} className="eval-diagnosis-item">
                <div className="eval-diagnosis-head">
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
                    {row.report_segment_id
                      ? t("evalReportReady")
                      : t("evalReportPending")}
                  </span>
                  <button
                    type="button"
                    className="eval-link"
                    onClick={() => onOpenConversation(row.session_id)}
                  >
                    {t("evalOpenSession")}
                  </button>
                  {target ? (
                    <button
                      type="button"
                      className="eval-link"
                      disabled={busy}
                      onClick={() => onStart(target, true)}
                    >
                      {t("evalDiagnosisForce")}
                    </button>
                  ) : null}
                </div>
                {row.report_segment_id ? (
                  <details className="eval-report-details">
                    <summary>{t("evalReportSections")}</summary>
                    <Report report={row.report} />
                  </details>
                ) : null}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
