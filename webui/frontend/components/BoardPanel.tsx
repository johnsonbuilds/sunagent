"use client";

import { STATUS_LABEL_KEYS, STATUS_ORDER } from "@/lib/board";
import { useI18n } from "@/lib/i18n";
import { useBoardStore } from "@/lib/stores/board";

import { PlusIcon, SearchIcon } from "./Icons";

export default function BoardPanel() {
  const { t } = useI18n();
  const {
    snapshot,
    loading,
    filter,
    setFilter,
    hideDone,
    setHideDone,
    openCreateTask,
  } = useBoardStore();

  const statuses = snapshot?.statuses ?? STATUS_ORDER;
  const counts = new Map<string, number>();
  for (const task of snapshot?.tasks ?? []) {
    counts.set(task.status, (counts.get(task.status) ?? 0) + 1);
  }

  return (
    <div className="panel">
      <div className="sidebar-head">
        <span className="sidebar-label">{t("boardTitle")}</span>
        <button
          className="icon-btn small"
          type="button"
          title={t("boardNewTask")}
          aria-label={t("boardNewTask")}
          onClick={() => openCreateTask("backlog")}
        >
          <PlusIcon />
        </button>
      </div>

      <div className="sidebar-search">
        <SearchIcon />
        <input
          type="search"
          placeholder={t("boardSearch")}
          autoComplete="off"
          spellCheck={false}
          value={filter}
          onChange={(event) => setFilter(event.target.value)}
        />
      </div>

      <label className="board-toggle">
        <input
          type="checkbox"
          checked={hideDone}
          onChange={(event) => setHideDone(event.target.checked)}
        />
        <span>{t("boardHideDone")}</span>
      </label>

      <div className="board-counts">
        <span className="sidebar-label">{t("boardColumns")}</span>
        {statuses.map((status) => (
          <div className="board-count-row" key={status}>
            <span>{t(STATUS_LABEL_KEYS[status])}</span>
            <span className="board-count">{counts.get(status) ?? 0}</span>
          </div>
        ))}
      </div>

      <div className="board-panel-foot">
        {loading ? <span>{t("boardLoading")}</span> : null}
      </div>
    </div>
  );
}
