"use client";

import { useEffect } from "react";

import { useI18n } from "@/lib/i18n";
import {
  NO_VALUE,
  formatBytes,
  formatCount,
  formatDayTime,
  formatMs,
  formatRatio,
  formatTokens,
} from "@/lib/evaluation";
import type {
  EvalDiagnosisRow,
  EvalDiagnosisTarget,
  EvalRunDetail,
  EvalTaskDetail,
} from "@/lib/types";

import MarkdownAnswer from "./MarkdownAnswer";
import RunTimeline from "./RunTimeline";

interface TaskDetailDrawerProps {
  open: boolean;
  detail: EvalTaskDetail | null;
  loading: boolean;
  run: EvalRunDetail | null;
  runId: string | null;
  runLoading: boolean;
  /** A diagnosis request is in flight: the triggers must not fire twice. */
  diagnosing: boolean;
  onClose: () => void;
  onOpenRun: (runId: string) => void;
  onCloseRun: () => void;
  onOpenConversation: (taskId: string) => void;
  onOpenConversationById: (sessionId: string) => void;
  onStartDiagnosis: (target: EvalDiagnosisTarget, force?: boolean) => void;
}

function EvidenceBadge({ value }: { value: string }) {
  const { t } = useI18n();
  const label = value === "present"
    ? t("evalEvidencePresent")
    : value === "none"
      ? t("evalEvidenceNone")
      : t("evalEvidenceMissing");
  return <span className={`eval-badge eval-badge-${value}`}>{label}</span>;
}

function Diagnoses({
  rows,
  busy,
  onStartDiagnosis,
  onOpenConversationById,
}: {
  rows: EvalDiagnosisRow[];
  busy: boolean;
  onStartDiagnosis: (target: EvalDiagnosisTarget, force?: boolean) => void;
  onOpenConversationById: (sessionId: string) => void;
}) {
  const { t } = useI18n();
  if (rows.length === 0) {
    return <div className="eval-chart-empty">{t("evalNoDiagnoses")}</div>;
  }
  return (
    <ul className="eval-diagnoses">
      {rows.map((row) => {
        const target = diagnosisTarget(row);
        return (
          <li key={row.session_id} className="eval-diagnosis-item">
            <span className="eval-diagnosis-title">{row.title || row.session_id}</span>
            <span className="eval-diagnosis-meta">
              {row.origin} · {formatDayTime(row.updated_at)}
              {row.report_segment_id
                ? ` · ${t("evalReportReady")}`
                : ` · ${t("evalReportPending")}`}
            </span>
            <button
              type="button"
              className="eval-link"
              onClick={() => onOpenConversationById(row.session_id)}
            >
              {t("evalOpenSession")}
            </button>
            {target ? (
              <button
                type="button"
                className="eval-link"
                disabled={busy}
                onClick={() => onStartDiagnosis(target, true)}
              >
                {t("evalDiagnosisForce")}
              </button>
            ) : null}
            {row.report_segment_id && row.report ? (
              <details className="eval-report-details">
                <summary>{t("evalReportSections")}</summary>
                <MarkdownAnswer report={row.report} />
              </details>
            ) : null}
          </li>
        );
      })}
    </ul>
  );
}

/** The target a stored diagnosis row was opened for, if it is still known. */
function diagnosisTarget(row: EvalDiagnosisRow): EvalDiagnosisTarget | null {
  const target = row.target as { type?: unknown; id?: unknown } | null;
  const type = target?.type;
  if ((type === "task" || type === "run")
      && typeof target?.id === "string" && target.id) {
    return { type, id: target.id };
  }
  return null;
}

export default function TaskDetailDrawer({
  open,
  detail,
  loading,
  run,
  runId,
  runLoading,
  diagnosing,
  onClose,
  onOpenRun,
  onCloseRun,
  onOpenConversation,
  onOpenConversationById,
  onStartDiagnosis,
}: TaskDetailDrawerProps) {
  const { t } = useI18n();

  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (!open) return null;

  return (
    <div className="eval-drawer-backdrop" onClick={onClose}>
      <aside
        className="eval-drawer"
        role="dialog"
        aria-modal="true"
        aria-label={detail?.title ?? t("evalTaskDetail")}
        onClick={(event) => event.stopPropagation()}
      >
        <header className="eval-drawer-head">
          <div>
            <h2 className="eval-drawer-title">{detail?.title ?? t("evalLoading")}</h2>
            <span className="eval-drawer-sub">
              {detail ? `${detail.task_id} · ${detail.status}` : ""}
            </span>
          </div>
          <div className="eval-drawer-actions">
            <button
              type="button"
              className="eval-link"
              onClick={() => detail && onOpenConversation(detail.task_id)}
            >
              {t("evalOpenConversation")}
            </button>
            <button type="button" className="eval-link" onClick={onClose}>
              {t("close")}
            </button>
          </div>
        </header>

        <div className="eval-drawer-body">
          {loading && !detail ? (
            <div className="eval-chart-empty">{t("evalLoading")}</div>
          ) : null}

          {detail ? (
            <>
              <dl className="eval-facts">
                <dt>{t("evalHarness")}</dt>
                <dd>{detail.harness ?? NO_VALUE}</dd>
                <dt>{t("evalModel")}</dt>
                <dd>{detail.model ?? NO_VALUE}</dd>
                <dt>{t("evalExpectsArtifact")}</dt>
                <dd>
                  {detail.expects_artifact === null
                    ? t("evalAuto")
                    : detail.expects_artifact
                      ? t("evalYes")
                      : t("evalNo")}
                  <span className="eval-fact-source">
                    {detail.expects_artifact_source === "explicit"
                      ? t("evalExplicit")
                      : t("evalFromHarness")}
                  </span>
                </dd>
                <dt>{t("evalExecuted")}</dt>
                <dd>
                  {detail.executed ? t("evalYes") : t("evalNo")} ·{" "}
                  {detail.done ? t("evalAccepted") : t("evalNotAccepted")}
                </dd>
                <dt>{t("evalColRework")}</dt>
                <dd>
                  {formatCount(detail.rework_count)}
                  {Object.keys(detail.rework_reasons).length
                    ? ` (${Object.entries(detail.rework_reasons)
                        .map(([reason, count]) => `${reason}×${count}`)
                        .join(", ")})`
                    : ""}
                </dd>
                <dt>{t("evalQueueWait")}</dt>
                <dd>
                  {detail.queue_waits_ms.length
                    ? formatMs(
                        detail.queue_waits_ms.reduce((a, b) => a + b, 0) /
                          detail.queue_waits_ms.length,
                      )
                    : NO_VALUE}
                </dd>
                <dt>{t("evalCreated")}</dt>
                <dd>{formatDayTime(detail.created_at)}</dd>
              </dl>

              {detail.description ? (
                <p className="eval-description">{detail.description}</p>
              ) : null}

              <h3 className="eval-subhead">{t("evalRuns")}</h3>
              {detail.runs.length === 0 ? (
                <div className="eval-chart-empty">{t("evalNoRuns")}</div>
              ) : (
                <ul className="eval-run-list">
                  {detail.runs.map((item) => {
                    const metrics = item.metrics;
                    return (
                      <li
                        key={item.run_id ?? `run-${item.finished_at}`}
                        className="eval-run-item"
                      >
                        <div className="eval-run-head">
                          <code className="eval-run-id">
                            {item.run_id ?? NO_VALUE}
                          </code>
                          <span className={`eval-badge eval-badge-${item.status}`}>
                            {item.status ?? NO_VALUE}
                          </span>
                          <EvidenceBadge value={item.evidence} />
                          <span className="eval-run-meta">
                            {metrics
                              ? `${formatTokens(metrics.tokens.total)} tok · ` +
                                `${formatMs(metrics.duration_ms)} · ` +
                                `${metrics.iterations} it`
                              : t("evalNoMetrics")}
                          </span>
                          {item.run_id && item.trace_available ? (
                            <button
                              type="button"
                              className="eval-link"
                              onClick={() => onOpenRun(item.run_id as string)}
                            >
                              {t("evalViewTrace")}
                            </button>
                          ) : null}
                          {item.run_id ? (
                            <button
                              type="button"
                              className="eval-link"
                              disabled={diagnosing}
                              onClick={() =>
                                onStartDiagnosis({
                                  type: "run",
                                  id: item.run_id as string,
                                })
                              }
                            >
                              {t("evalDiagnosisRun")}
                            </button>
                          ) : null}
                        </div>
                        {item.error ? (
                          <div className="eval-run-error">{item.error}</div>
                        ) : null}
                        {item.edited_files.length ? (
                          <div className="eval-run-files">
                            {item.edited_files.slice(0, 6).join(", ")}
                            {item.edited_files.length > 6
                              ? ` +${item.edited_files.length - 6}`
                              : ""}
                          </div>
                        ) : null}
                      </li>
                    );
                  })}
                </ul>
              )}

              {runId ? (
                <div className="eval-run-detail">
                  <div className="eval-run-detail-head">
                    <h3 className="eval-subhead">{t("evalTraceDigest")}</h3>
                    <button type="button" className="eval-link" onClick={onCloseRun}>
                      {t("evalHide")}
                    </button>
                  </div>
                  {runLoading && !run ? (
                    <div className="eval-chart-empty">{t("evalLoading")}</div>
                  ) : null}
                  {run ? (
                    <>
                      <dl className="eval-facts">
                        <dt>{t("evalKpiToolCalls")}</dt>
                        <dd>
                          {formatCount(run.metrics.tool_calls)}
                          {run.metrics.tool_errors
                            ? ` · ${t("evalErrorsN", {
                                count: run.metrics.tool_errors,
                              })}`
                            : ""}
                        </dd>
                        <dt>{t("evalTokens")}</dt>
                        <dd>
                          {formatTokens(run.metrics.tokens.prompt)} /{" "}
                          {formatTokens(run.metrics.tokens.completion)} /{" "}
                          {formatTokens(run.metrics.tokens.cached)}
                        </dd>
                        <dt>{t("evalKpiTtftP50")}</dt>
                        <dd>{formatMs(run.metrics.ttft_ms)}</dd>
                        <dt>{t("evalLatency")}</dt>
                        <dd>
                          {Object.entries(run.metrics.latency_ms)
                            .map(([key, value]) => `${key} ${formatMs(value)}`)
                            .join(" · ")}
                        </dd>
                        <dt>{t("evalTrace")}</dt>
                        <dd>
                          {run.digest.available
                            ? `${formatCount(run.digest.events)} · ` +
                              `${formatBytes(run.digest.bytes)}` +
                              (run.digest.truncated ? ` · ${t("evalTruncated")}` : "")
                            : (run.digest.reason ?? t("evalTraceUnavailable"))}
                        </dd>
                      </dl>
                      {run.digest.available && run.digest.tools?.length ? (
                        <ul className="eval-digest-tools">
                          {run.digest.tools.slice(0, 10).map((tool) => (
                            <li key={tool.tool}>
                              <span>{tool.tool}</span>
                              <span>
                                {formatCount(tool.calls)} ·{" "}
                                {tool.errors
                                  ? formatRatio(tool.errors / tool.calls)
                                  : formatRatio(0)}
                              </span>
                            </li>
                          ))}
                        </ul>
                      ) : null}
                      {run.digest.error_samples?.length ? (
                        <ul className="eval-digest-errors">
                          {run.digest.error_samples.slice(0, 5).map((sample, i) => (
                            <li key={`${sample.tool}-${i}`}>
                              {sample.tool}
                              {sample.error_kind ? `: ${sample.error_kind}` : ""}
                              {sample.iteration !== null
                                ? ` @${sample.iteration}`
                                : ""}
                            </li>
                          ))}
                        </ul>
                      ) : null}
                    </>
                  ) : null}
                </div>
              ) : null}

              <h3 className="eval-subhead">{t("evalTimeline")}</h3>
              <RunTimeline
                events={detail.timeline}
                emptyLabel={t("evalNoTimeline")}
              />

              <h3 className="eval-subhead">{t("evalDiagnoses")}</h3>
              <button
                type="button"
                className="eval-action"
                disabled={diagnosing}
                onClick={() =>
                  onStartDiagnosis({ type: "task", id: detail.task_id })
                }
              >
                {diagnosing ? t("evalDiagnosisBusy") : t("evalDiagnosisStart")}
              </button>
              <Diagnoses
                rows={detail.diagnoses}
                busy={diagnosing}
                onStartDiagnosis={onStartDiagnosis}
                onOpenConversationById={onOpenConversationById}
              />
            </>
          ) : null}
        </div>
      </aside>
    </div>
  );
}
