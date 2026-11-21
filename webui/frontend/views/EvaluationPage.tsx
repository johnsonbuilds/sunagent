"use client";

import { useRouter } from "next/navigation";
import { useMemo, useState } from "react";
import EvaluationFunnel from "@/components/evaluation/EvaluationFunnel";
import EvaluationKpis, {
  ratioCard,
  type KpiCard,
} from "@/components/evaluation/EvaluationKpis";
import BarList from "@/components/evaluation/BarList";
import DiagnosisPanel from "@/components/evaluation/DiagnosisPanel";
import Histogram from "@/components/evaluation/Histogram";
import Section from "@/components/evaluation/Section";
import StackedBars from "@/components/evaluation/StackedBars";
import TaskDetailDrawer from "@/components/evaluation/TaskDetailDrawer";
import TaskTable from "@/components/evaluation/TaskTable";
import TrendChart from "@/components/evaluation/TrendChart";
import { api } from "@/lib/api";
import {
  EVAL_ORIGINS,
  EVAL_RANGES,
  EVAL_TABS,
  NO_VALUE,
  formatBytes,
  formatCount,
  formatDayTime,
  formatMs,
  formatRatio,
  formatTokens,
  shortDay,
  trimSeries,
} from "@/lib/evaluation";
import { useI18n, type MessageKey } from "@/lib/i18n";
import { useSessions } from "@/lib/stores/sessions";
import { useRuntimeConfig } from "@/lib/stores/runtime-config";
import { useToast } from "@/lib/stores/toast";
import { useEvaluation, useStickyOptions } from "@/lib/useEvaluation";
import type {
  EnsureSessionResult,
  EvalDiagnosisTarget,
  EvalTab,
} from "@/lib/types";

const TAB_LABELS: Record<EvalTab, MessageKey> = {
  overview: "evalTabOverview",
  validity: "evalTabValidity",
  process: "evalTabProcess",
  efficiency: "evalTabEfficiency",
  reliability: "evalTabReliability",
  tasks: "evalTabTasks",
  benchmarks: "evalTabBenchmarks",
  diagnosis: "evalTabDiagnosis",
};

const RANGE_LABELS: Record<string, MessageKey> = {
  "24h": "evalRange24h",
  "7d": "evalRange7d",
  "30d": "evalRange30d",
  "90d": "evalRange90d",
  all: "evalRangeAll",
};

export default function EvaluationPage() {
  const { t } = useI18n();
  const router = useRouter();
  const ev = useEvaluation();
  const { harnesses, models } = useRuntimeConfig();
  const { refreshSessions } = useSessions();
  const { showToast } = useToast();
  const [tab, setTab] = useState<EvalTab>("overview");

  const overview = ev.overview;
  const board = ev.board;
  const kpis = overview?.kpis;
  const boardKpis = board?.kpis;

  const openConversation = (sessionId: string) => {
    router.push(`/?session=${encodeURIComponent(sessionId)}`);
  };

  const openTaskConversation = (taskId: string) => {
    void api<EnsureSessionResult>(
      `/api/board/tasks/${encodeURIComponent(taskId)}/session`,
      { method: "POST", body: "{}" },
    )
      .then(async (result) => {
        await refreshSessions();
        openConversation(result.session_id);
      })
      .catch((failure: unknown) => {
        showToast(
          `${t("evalOpenConversationFailed")} ` +
            (failure instanceof Error ? failure.message : String(failure)),
          "error",
        );
      });
  };

  const startDiagnosis = (target: EvalDiagnosisTarget, force = false) => {
    // Manual only: nothing on this page starts a diagnosis by itself.
    void ev
      .startDiagnosis(target, force)
      .then(async (result) => {
        await refreshSessions();
        if (result.duplicate && result.existing_session_id) {
          // The toast says the existing session was opened; open it.
          showToast(t("evalDiagnosisDuplicate"), "info");
          openConversation(result.existing_session_id);
          return;
        }
        showToast(t("evalDiagnosisStarted"), "success");
      })
      .catch((failure: unknown) => {
        showToast(
          `${t("evalDiagnosisFailed")} ` +
            (failure instanceof Error ? failure.message : String(failure)),
          "error",
        );
      });
  };

  const cohortFilters = useMemo(() => {
    const filters: Record<string, string> = { range: ev.filters.range };
    if (ev.filters.origin) filters.origin = ev.filters.origin;
    if (ev.filters.harness) filters.harness = ev.filters.harness;
    if (ev.filters.model) filters.model = ev.filters.model;
    return filters;
  }, [ev.filters]);

  const bestAccuracy = useMemo(() => {
    const values = Object.values(ev.benchmarks?.latest_accuracy ?? {});
    if (!values.length) return null;
    return Math.max(...values);
  }, [ev.benchmarks]);

  const harnessOptions = useStickyOptions(
    (overview?.slices.harness ?? []).map((row) => row.key),
    harnesses.map((item) => item.id),
  );

  const modelOptions = useStickyOptions(
    (overview?.slices.model ?? []).map((row) => row.key),
    models.map((item) => item.id),
  );

  const funnel = board
    ? [
        { label: t("evalFunnelCreated"), value: board.funnel.created },
        { label: t("evalFunnelExecuted"), value: board.funnel.executed },
        { label: t("evalFunnelRunCompleted"), value: board.funnel.run_completed },
        { label: t("evalFunnelReview"), value: board.funnel.review },
        { label: t("evalFunnelDone"), value: board.funnel.done },
      ]
    : [];

  const pageCards: KpiCard[] = [
    ratioCard(t("evalKpiTsr"), boardKpis?.tsr, { hint: t("evalBoardView") }),
    ratioCard(t("evalKpiFirstPass"), boardKpis?.first_pass_rate),
    ratioCard(t("evalKpiRework"), boardKpis?.rework_rate, { invert: true }),
    ratioCard(t("evalKpiTcr"), kpis?.tcr, { hint: t("evalRunView") }),
    ratioCard(t("evalKpiVcr"), boardKpis?.vcr),
    {
      label: t("evalKpiTokensPerTask"),
      value: formatTokens(boardKpis?.tokens_per_task),
    },
    {
      label: t("evalKpiP95"),
      value: formatMs(kpis?.duration_p95_ms),
    },
    {
      label: t("evalKpiRunsPerTask"),
      value: boardKpis?.runs_per_task === null || boardKpis?.runs_per_task === undefined
        ? NO_VALUE
        : boardKpis.runs_per_task.toFixed(2),
    },
    {
      label: t("evalKpiAccuracy"),
      value: formatRatio(bestAccuracy),
      hint: t("evalHarborView"),
    },
  ];

  const tokenTone = (key: string) => key;

  return (
    <div className="evaluation">
      <header className="eval-head">
        <div className="eval-head-left">
          <h1 className="eval-heading">{t("navEvaluation")}</h1>
          <span className="eval-subtitle">{t("evalSubtitle")}</span>
        </div>
        <div className="eval-filters">
          <select
            className="eval-select"
            value={ev.filters.range}
            onChange={(event) => ev.setFilters({ range: event.target.value })}
            aria-label={t("evalRange")}
          >
            {EVAL_RANGES.map((range) => (
              <option key={range} value={range}>
                {t(RANGE_LABELS[range])}
              </option>
            ))}
          </select>
          <select
            className="eval-select"
            value={ev.filters.origin}
            onChange={(event) => ev.setFilters({ origin: event.target.value })}
            aria-label={t("evalOrigin")}
          >
            <option value="">{t("evalAllOrigins")}</option>
            {EVAL_ORIGINS.map((origin) => (
              <option key={origin} value={origin}>
                {origin}
              </option>
            ))}
          </select>
          <select
            className="eval-select"
            value={ev.filters.harness}
            onChange={(event) => ev.setFilters({ harness: event.target.value })}
            aria-label={t("evalHarness")}
          >
            <option value="">{t("evalAllHarness")}</option>
            {harnessOptions.map((harness) => (
              <option key={harness} value={harness}>
                {harness}
              </option>
            ))}
          </select>
          <select
            className="eval-select"
            value={ev.filters.model}
            onChange={(event) => ev.setFilters({ model: event.target.value })}
            aria-label={t("evalModel")}
          >
            <option value="">{t("evalAllModels")}</option>
            {modelOptions.map((model) => (
              <option key={model} value={model}>
                {model}
              </option>
            ))}
          </select>
          <button
            type="button"
            className="eval-btn"
            onClick={ev.refresh}
            disabled={ev.loading}
          >
            {t("evalRefresh")}
          </button>
        </div>
      </header>

      <div className="eval-scroll">
        {ev.error ? (
          <div className="eval-error">
            <span>{t("evalLoadFailed")}</span>
            <span className="eval-error-detail">{ev.error}</span>
            <button type="button" className="eval-link" onClick={ev.refresh}>
              {t("evalRetry")}
            </button>
          </div>
        ) : null}

        <EvaluationKpis cards={pageCards} />

        <nav className="eval-tabs" aria-label={t("navEvaluation")}>
          {EVAL_TABS.map((item) => (
            <button
              key={item}
              type="button"
              className={`eval-tab${tab === item ? " active" : ""}`}
              aria-current={tab === item ? "page" : undefined}
              onClick={() => setTab(item)}
            >
              {t(TAB_LABELS[item])}
            </button>
          ))}
        </nav>

        {tab === "overview" ? (
          <div className="eval-grid">
            <Section title={t("evalFunnel")} hint={t("evalFunnelHint")}>
              {funnel.length ? (
                <EvaluationFunnel steps={funnel} />
              ) : (
                <div className="eval-chart-empty">{t("evalLoading")}</div>
              )}
            </Section>
            <Section title={t("evalTsrTrend")}>
              <TrendChart
                points={trimSeries(board?.tsr_trend ?? []).map((row) => ({
                  label: shortDay(row.day),
                  value: row.tsr,
                }))}
                format={(value) => formatRatio(value)}
                emptyLabel={t("evalEmpty")}
              />
            </Section>
            <Section title={t("evalTcrTrend")}>
              <TrendChart
                points={trimSeries(overview?.trends ?? []).map((row) => ({
                  label: shortDay(row.day),
                  value: row.tcr,
                }))}
                format={(value) => formatRatio(value)}
                emptyLabel={t("evalEmpty")}
              />
            </Section>
            <Section title={t("evalReworkReasons")}>
              <BarList
                rows={(board?.rework_reasons ?? []).map((row) => ({
                  label: row.reason,
                  value: row.count,
                }))}
                format={formatCount}
                emptyLabel={t("evalEmpty")}
              />
            </Section>
            <Section title={t("evalTopTokens")}>
              <BarList
                rows={(board?.top_offenders.tokens ?? []).map((row) => ({
                  label: row.task_id,
                  value: row.tokens,
                  hint: `· ${row.runs} runs`,
                }))}
                format={formatTokens}
                emptyLabel={t("evalEmpty")}
              />
            </Section>
            <Section title={t("evalTopWall")}>
              <BarList
                rows={(board?.top_offenders.wall_ms ?? []).map((row) => ({
                  label: row.task_id,
                  value: row.wall_ms,
                  hint: row.status,
                }))}
                format={formatMs}
                emptyLabel={t("evalEmpty")}
              />
            </Section>
            <Section title={t("evalTopRework")}>
              <BarList
                rows={(board?.top_offenders.rework ?? []).map((row) => ({
                  label: row.task_id,
                  value: row.rework,
                  hint: `· ${row.runs} runs`,
                }))}
                format={formatCount}
                emptyLabel={t("evalEmpty")}
              />
            </Section>
            <Section title={t("evalDataQuality")}>
              <dl className="eval-facts">
                <dt>{t("evalExcludedRuns")}</dt>
                <dd>{formatCount(overview?.excluded.runs_by_origin)}</dd>
                <dt>{t("evalDroppedEvents")}</dt>
                <dd>{formatCount(kpis?.dropped_events)}</dd>
                <dt>{t("evalTraceBytes")}</dt>
                <dd>{formatBytes(kpis?.trace_bytes)}</dd>
                <dt>{t("evalKpiPolicyBlocks")}</dt>
                <dd>{formatCount(kpis?.policy_blocks)}</dd>
              </dl>
            </Section>
          </div>
        ) : null}

        {tab === "validity" ? (
          <div className="eval-grid">
            <Section title={t("evalArtifact")} hint={t("evalArtifactHint")}>
              <dl className="eval-facts">
                <dt>{t("evalJudgedTasks")}</dt>
                <dd>{formatCount(board?.artifact.judged_tasks)}</dd>
                <dt>{t("evalClaimingTasks")}</dt>
                <dd>{formatCount(board?.artifact.claiming_tasks)}</dd>
                <dt>{t("evalUnmeasured")}</dt>
                <dd>{formatCount(board?.artifact.unmeasured_claiming_tasks)}</dd>
                <dt>{t("evalKpiVcr")}</dt>
                <dd>
                  {formatRatio(board?.artifact.vcr)}
                  <span className="eval-fact-source">
                    {`${formatCount(board?.artifact.vcr_passed)} / ` +
                      `${formatCount(board?.artifact.vcr_runs)}`}
                  </span>
                </dd>
                <dt>{t("evalKpiHallucinated")}</dt>
                <dd>
                  {formatCount(board?.artifact.hallucinated_count)} ·{" "}
                  {formatRatio(board?.artifact.hallucinated_rate)}
                </dd>
              </dl>
              {board?.artifact.hallucinated_tasks.length ? (
                <ul className="eval-diagnoses">
                  {board.artifact.hallucinated_tasks.map((row) => (
                    <li key={`${row.task_id}-${row.run_id}`} className="eval-diagnosis-item">
                      <button
                        type="button"
                        className="eval-task-link"
                        onClick={() => void ev.openTask(row.task_id)}
                      >
                        {row.task_id}
                      </button>
                      <span className="eval-diagnosis-meta">
                        {row.status} · {row.runs} runs · {row.rework_count} rework
                      </span>
                      {row.run_id ? (
                        <button
                          type="button"
                          className="eval-link"
                          onClick={() =>
                            void ev.openTask(row.task_id, row.run_id ?? undefined)
                          }
                        >
                          {t("evalViewTrace")}
                        </button>
                      ) : null}
                    </li>
                  ))}
                </ul>
              ) : null}
            </Section>
            <Section title={t("evalReworkReasons")}>
              <BarList
                rows={(board?.rework_reasons ?? []).map((row) => ({
                  label: row.reason,
                  value: row.count,
                }))}
                format={formatCount}
                emptyLabel={t("evalEmpty")}
              />
            </Section>
            <Section title={t("evalSliceHarness")}>
              <BarList
                rows={(overview?.slices.harness ?? []).map((row) => ({
                  label: row.key || "(none)",
                  value: row.tcr,
                  hint: `${row.runs} runs`,
                }))}
                format={(value) => formatRatio(value)}
                emptyLabel={t("evalEmpty")}
              />
            </Section>
            <Section title={t("evalSliceModel")}>
              <BarList
                rows={(overview?.slices.model ?? []).map((row) => ({
                  label: row.key || "(none)",
                  value: row.tcr,
                  hint: `${row.runs} runs`,
                }))}
                format={(value) => formatRatio(value)}
                emptyLabel={t("evalEmpty")}
              />
            </Section>
          </div>
        ) : null}

        {tab === "process" ? (
          <div className="eval-grid">
            <Section title={t("evalErrorKinds")} hint={t("evalProcessHint")}>
              <BarList
                rows={(overview?.error_kinds ?? []).map((row) => ({
                  label: row.kind,
                  value: row.count,
                }))}
                format={formatCount}
                emptyLabel={t("evalEmpty")}
              />
            </Section>
            <Section title={t("evalIterations")}>
              <Histogram
                rows={overview?.histograms.iterations ?? []}
                emptyLabel={t("evalEmpty")}
              />
            </Section>
            <Section title={t("evalErrorsByStage")}>
              <BarList
                rows={(overview?.errors ?? []).map((row) => ({
                  label: `${row.stage || "—"} / ${row.code || "—"}`,
                  value: row.count,
                }))}
                format={formatCount}
                emptyLabel={t("evalEmpty")}
              />
            </Section>
            <Section title={t("evalSlices")}>
              <dl className="eval-facts">
                <dt>{t("evalKpiToolCalls")}</dt>
                <dd>{formatCount(kpis?.tool_calls)}</dd>
                <dt>{t("evalKpiToolError")}</dt>
                <dd>{formatRatio(kpis?.tool_error_rate)}</dd>
                <dt>{t("evalKpiSchemaPass")}</dt>
                <dd>{formatRatio(kpis?.schema_pass_rate)}</dd>
                <dt>{t("evalKpiPolicyBlocks")}</dt>
                <dd>{formatCount(kpis?.policy_blocks)}</dd>
              </dl>
            </Section>
          </div>
        ) : null}

        {tab === "efficiency" ? (
          <div className="eval-grid">
            <Section title={t("evalTokenMix")}>
              <StackedBars
                rows={
                  overview
                    ? [
                        {
                          label: t("evalKpiTokens"),
                          segments: [
                            { label: t("evalPrompts"), value: overview.tokens.prompt, tone: tokenTone("prompt") },
                            { label: t("evalCompletion"), value: overview.tokens.completion, tone: tokenTone("completion") },
                            { label: t("evalSummarizer"), value: overview.tokens.summarizer, tone: tokenTone("summarizer") },
                          ],
                        },
                      ]
                    : []
                }
                format={formatTokens}
                emptyLabel={t("evalEmpty")}
              />
            </Section>
            <Section title={t("evalLatencySplit")}>              <StackedBars
                rows={
                  overview
                    ? [
                        {
                          label: t("evalLatency"),
                          segments: [
                            { label: t("evalLlm"), value: overview.latency_ms.llm, tone: "prompt" },
                            { label: t("evalTool"), value: overview.latency_ms.tool, tone: "completion" },
                            { label: t("evalSummarizer"), value: overview.latency_ms.summarizer, tone: "summarizer" },
                            { label: t("evalOverhead"), value: overview.latency_ms.overhead, tone: "overhead" },
                          ],
                        },
                      ]
                    : []
                }
                format={formatMs}
                emptyLabel={t("evalEmpty")}
              />
            </Section>
            <Section title={t("evalCache")}>
              <dl className="eval-facts">
                <dt>{t("evalCachedTokens")}</dt>
                <dd>{formatTokens(overview?.tokens.cached)}</dd>
                <dt>{t("evalCacheHitRate")}</dt>
                <dd>
                  {formatRatio(
                    overview && overview.tokens.prompt > 0
                      ? overview.tokens.cached / overview.tokens.prompt
                      : null,
                  )}
                  <span className="eval-fact-source">{t("evalCachedHint")}</span>
                </dd>
              </dl>
            </Section>
            <Section title={t("evalTokensTrend")}>
              <TrendChart
                points={trimSeries(overview?.trends ?? []).map((row) => ({
                  label: shortDay(row.day),
                  value: row.tokens,
                }))}
                format={formatTokens}
                emptyLabel={t("evalEmpty")}
              />
            </Section>
            <Section title={t("evalDuration")}>
              <Histogram
                rows={overview?.histograms.duration_ms ?? []}
                emptyLabel={t("evalEmpty")}
              />
            </Section>
            <Section title={t("evalKpiP95")}>
              <dl className="eval-facts">
                <dt>{t("evalKpiDurationP50")}</dt>
                <dd>{formatMs(kpis?.duration_p50_ms)}</dd>
                <dt>{t("evalKpiP95")}</dt>
                <dd>{formatMs(kpis?.duration_p95_ms)}</dd>
                <dt>{t("evalKpiTtftP50")}</dt>
                <dd>{formatMs(kpis?.ttft_p50_ms)}</dd>
                <dt>{t("evalKpiTtftP95")}</dt>
                <dd>{formatMs(kpis?.ttft_p95_ms)}</dd>
                <dt>{t("evalKpiQueueWait")}</dt>
                <dd>
                  {formatMs(boardKpis?.queue_wait_p50_ms)} /{" "}
                  {formatMs(boardKpis?.queue_wait_p95_ms)}
                </dd>
                <dt>{t("evalKpiTokensPerTask")}</dt>
                <dd>{formatTokens(boardKpis?.tokens_per_task)}</dd>
              </dl>
            </Section>
            <Section title={t("evalTopTokens")}>
              <BarList
                rows={(board?.top_offenders.tokens ?? []).map((row) => ({
                  label: row.task_id,
                  value: row.tokens,
                  hint: `· ${row.runs} runs`,
                }))}
                format={formatTokens}
                emptyLabel={t("evalEmpty")}
              />
            </Section>
          </div>
        ) : null}

        {tab === "reliability" ? (
          <div className="eval-grid">
            <Section title={t("evalReliabilityKpis")}>
              <dl className="eval-facts">
                <dt>{t("evalKpiErrorRate")}</dt>
                <dd>{formatRatio(kpis?.error_rate)}</dd>
                <dt>{t("evalKpiCancelled")}</dt>
                <dd>{formatRatio(kpis?.cancelled_rate)}</dd>
                <dt>{t("evalKpiRetry")}</dt>
                <dd>{formatRatio(kpis?.retry_rate)}</dd>
                <dt>{t("evalKpiSummaryInvalid")}</dt>
                <dd>{formatRatio(kpis?.summary_invalid_rate)}</dd>
                <dt>{t("evalRetries")}</dt>
                <dd>
                  {formatCount(overview?.retries.runs)} /{" "}
                  {formatCount(overview?.retries.total)}
                </dd>
              </dl>
            </Section>
            <Section title={t("evalSummary")}>
              <dl className="eval-facts">
                <dt>{t("evalAttempts")}</dt>
                <dd>{formatCount(overview?.summary.attempts)}</dd>
                <dt>{t("evalInvalid")}</dt>
                <dd>{formatCount(overview?.summary.invalid)}</dd>
                <dt>{t("evalCompactions")}</dt>
                <dd>{formatCount(overview?.summary.compactions)}</dd>
              </dl>
            </Section>
            <Section title={t("evalErrorsByStage")}>
              <BarList
                rows={(overview?.errors ?? []).map((row) => ({
                  label: `${row.stage || "—"} / ${row.code || "—"}`,
                  value: row.count,
                }))}
                format={formatCount}
                emptyLabel={t("evalEmpty")}
              />
            </Section>
            <Section title={t("evalDuration")}>
              <Histogram
                rows={overview?.histograms.duration_ms ?? []}
                emptyLabel={t("evalEmpty")}
              />
            </Section>
          </div>
        ) : null}

        {tab === "tasks" ? (
          <Section title={t("evalColTask")}>
            <TaskTable
              page={ev.tasks}
              query={ev.taskQuery}
              onQuery={ev.setTaskQuery}
              onOpen={(taskId) => void ev.openTask(taskId)}
              onOpenConversation={openTaskConversation}
              loading={ev.tasksLoading}
            />
          </Section>
        ) : null}

        {tab === "benchmarks" ? (
          <div className="eval-grid">
            <Section title={t("evalBenchmarks")}>
              <BarList
                rows={Object.entries(ev.benchmarks?.latest_accuracy ?? {})
                  .map(([harness, accuracy]) => ({
                    label: harness,
                    value: accuracy,
                  }))}
                format={(value) => formatRatio(value, 1)}
                emptyLabel={t("evalNoBenchmarks")}
              />
            </Section>
            <Section title={t("evalBenchmarkRecords")}>
              {ev.benchmarks?.records.length ? (
                <div className="eval-table-wrap">
                  <table className="eval-table">
                    <thead>
                      <tr>
                        <th>{t("evalColBenchmark")}</th>
                        <th>{t("evalHarness")}</th>
                        <th>{t("evalModel")}</th>
                        <th>{t("evalColAccuracy")}</th>
                        <th>{t("evalColTasks")}</th>
                        <th>{t("evalColCreated")}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {ev.benchmarks.records.slice(0, 50).map((row, index) => (
                        <tr key={row.record_id ?? `record-${index}`}>
                          <td title={row.job_dir ?? ""}>
                            {row.benchmark ?? NO_VALUE}
                          </td>
                          <td>{row.harness_id ?? NO_VALUE}</td>
                          <td>{row.model ?? NO_VALUE}</td>
                          <td className="eval-num">{formatRatio(row.accuracy, 1)}</td>
                          <td className="eval-num">{formatCount(row.tasks)}</td>
                          <td>{row.created_at ?? formatDayTime(null)}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              ) : (
                <div className="eval-chart-empty">{t("evalNoBenchmarks")}</div>
              )}
            </Section>
          </div>
        ) : null}

        {tab === "diagnosis" ? (
          <Section title={t("evalTabDiagnosis")} hint={t("evalDiagnosisHint")}>
            <DiagnosisPanel
              rows={ev.diagnoses}
              busy={ev.diagnosing}
              cohortFilters={cohortFilters}
              onStart={startDiagnosis}
              onOpenConversation={openConversation}
            />
          </Section>
        ) : null}
      </div>

      <TaskDetailDrawer
        open={ev.detailId !== null}
        detail={ev.detail}
        loading={ev.detailLoading}
        run={ev.run}
        runId={ev.runId}
        runLoading={ev.runLoading}
        diagnosing={ev.diagnosing}
        onClose={ev.closeTask}
        onOpenRun={(runId) => void ev.openRun(runId)}
        onCloseRun={ev.closeRun}
        onOpenConversation={openTaskConversation}
        onOpenConversationById={openConversation}
        onStartDiagnosis={startDiagnosis}
      />
    </div>
  );
}
