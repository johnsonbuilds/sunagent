"use client";

import { useI18n } from "@/lib/i18n";
import { STATUS_LABEL_KEYS } from "@/lib/board";
import { formatCount, formatDayTime, formatMs, formatTokens } from "@/lib/evaluation";
import type { TaskQuery } from "@/lib/useEvaluation";
import type { EvalTaskPage } from "@/lib/types";

interface TaskTableProps {
  page: EvalTaskPage | null;
  query: TaskQuery;
  onQuery: (patch: Partial<TaskQuery>) => void;
  onOpen: (taskId: string) => void;
  onOpenConversation: (taskId: string) => void;
  loading: boolean;
}

const SORTABLE: { key: TaskQuery["sort"]; labelKey: Parameters<
  ReturnType<typeof useI18n>["t"]>[0] }[] = [
  { key: "updated_at", labelKey: "evalColUpdated" },
  { key: "status", labelKey: "evalColStatus" },
  { key: "runs", labelKey: "evalColRuns" },
  { key: "tokens", labelKey: "evalColTokens" },
  { key: "wall_ms", labelKey: "evalColWall" },
  { key: "rework", labelKey: "evalColRework" },
];

const STATUS_OPTIONS = ["", "backlog", "todo", "doing", "review", "done"] as const;

export default function TaskTable({
  page,
  query,
  onQuery,
  onOpen,
  onOpenConversation,
  loading,
}: TaskTableProps) {
  const { t } = useI18n();
  const rows = page?.rows ?? [];
  const pageSize = page?.page_size ?? query.page_size;
  const pages = Math.max(1, Math.ceil((page?.total ?? 0) / pageSize));

  const sortBy = (key: TaskQuery["sort"]) => {
    onQuery({
      sort: key,
      order: query.sort === key && query.order === "desc" ? "asc" : "desc",
    });
  };

  return (
    <div className="eval-tasks">
      <div className="eval-task-filters">
        <input
          className="eval-input"
          type="search"
          value={query.q}
          placeholder={t("evalTaskSearch")}
          onChange={(event) => onQuery({ q: event.target.value })}
        />
        <select
          className="eval-select"
          value={query.status}
          onChange={(event) => onQuery({ status: event.target.value })}
          aria-label={t("evalColStatus")}
        >
          {STATUS_OPTIONS.map((status) => (
            <option key={status || "all"} value={status}>
              {status ? t(STATUS_LABEL_KEYS[status]) : t("evalAllStatuses")}
            </option>
          ))}
        </select>
        <span className="eval-task-total">
          {t("evalTaskTotal", { count: page?.total ?? 0 })}
        </span>
      </div>

      <div className="eval-table-wrap">
        <table className="eval-table">
          <thead>
            <tr>
              <th className="eval-col-title">{t("evalColTask")}</th>
              {SORTABLE.map((column) => (
                <th key={column.key}>
                  <button
                    type="button"
                    className={`eval-sort${
                      query.sort === column.key ? " active" : ""
                    }`}
                    onClick={() => sortBy(column.key)}
                  >
                    {t(column.labelKey)}
                    {query.sort === column.key
                      ? query.order === "desc" ? " ↓" : " ↑"
                      : ""}
                  </button>
                </th>
              ))}
              <th>{t("evalColLastError")}</th>
              <th className="eval-col-actions">{t("evalColActions")}</th>
            </tr>
          </thead>
          <tbody>
            {rows.length === 0 ? (
              <tr>
                <td colSpan={9} className="eval-table-empty">
                  {loading ? t("evalLoading") : t("evalNoTasks")}
                </td>
              </tr>
            ) : (
              rows.map((row) => (
                <tr key={row.task_id}>
                  <td className="eval-col-title">
                    <button
                      type="button"
                      className="eval-task-link"
                      onClick={() => onOpen(row.task_id)}
                      title={row.task_id}
                    >
                      {row.title}
                    </button>
                    <span className="eval-task-meta">
                      {row.harness || "—"}
                      {row.model ? ` · ${row.model}` : ""}
                    </span>
                  </td>
                  <td className="eval-col-updated">
                    {formatDayTime(row.updated_at)}
                  </td>
                  <td>
                    <span className={`eval-status eval-status-${row.status}`}>
                      {t(STATUS_LABEL_KEYS[row.status] ?? "evalColStatus")}
                    </span>
                  </td>
                  <td className="eval-num">{formatCount(row.runs)}</td>
                  <td className="eval-num">{formatTokens(row.tokens)}</td>
                  <td className="eval-num">{formatMs(row.wall_ms)}</td>
                  <td className="eval-num">{formatCount(row.rework)}</td>
                  <td className="eval-col-error" title={row.last_error ?? ""}>
                    {row.last_error ? row.last_error.slice(0, 60) : "—"}
                  </td>
                  <td className="eval-col-actions">
                    <button
                      type="button"
                      className="eval-link"
                      onClick={() => onOpen(row.task_id)}
                    >
                      {t("evalViewDetail")}
                    </button>
                    <button
                      type="button"
                      className="eval-link"
                      onClick={() => onOpenConversation(row.task_id)}
                    >
                      {t("evalOpenConversation")}
                    </button>
                  </td>
                </tr>
              ))
            )}
          </tbody>
        </table>
      </div>

      <div className="eval-pager">
        <button
          type="button"
          className="eval-link"
          disabled={query.page <= 1}
          onClick={() => onQuery({ page: query.page - 1 })}
        >
          {t("evalPrevPage")}
        </button>
        <span>
          {t("evalPageOf", {
            page: page?.page ?? query.page,
            pages,
          })}
        </span>
        <button
          type="button"
          className="eval-link"
          disabled={query.page >= pages}
          onClick={() => onQuery({ page: query.page + 1 })}
        >
          {t("evalNextPage")}
        </button>
        <select
          className="eval-select eval-page-size"
          value={query.page_size}
          onChange={(event) =>
            onQuery({ page_size: Number(event.target.value), page: 1 })
          }
          aria-label={t("evalPageSize")}
        >
          {[25, 50, 100].map((size) => (
            <option key={size} value={size}>
              {size}
            </option>
          ))}
        </select>
      </div>
    </div>
  );
}
