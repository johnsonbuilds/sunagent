"use client";

import { Fragment } from "react";

import LanguageIcon from "@/components/files/LanguageIcon";
import StatusBadge from "@/components/files/StatusBadge";
import TreeRow from "@/components/files/TreeRow";
import { formatSize, type ChangeIndex } from "@/lib/files";
import { useI18n } from "@/lib/i18n";
import type { DirState } from "@/lib/useFiles";
import type { WorkspaceEntry } from "@/lib/types";

export default function FileTree({
  dirs,
  expanded,
  selected,
  changes,
  onToggle,
  onSelect,
  onRetry,
}: {
  dirs: Record<string, DirState>;
  expanded: string[];
  selected: string | null;
  changes?: ChangeIndex;
  onToggle: (path: string) => void;
  onSelect: (entry: WorkspaceEntry) => void;
  onRetry: (path: string) => void;
}) {
  const { t } = useI18n();
  const open = new Set(expanded);
  const root = dirs[""];

  const renderDir = (path: string, depth: number) => {
    const state = dirs[path];
    if (!state) return null;
    if (state.status === "loading" && state.entries.length === 0) {
      return (
        <div className="files-row is-muted" style={{ paddingLeft: 8 + depth * 14 }}>
          <span className="files-chevron" />
          <span className="files-row-name">{t("filesLoading")}</span>
        </div>
      );
    }
    if (state.status === "error") {
      return (
        <div className="files-row is-error" style={{ paddingLeft: 8 + depth * 14 }}>
          <span className="files-chevron" />
          <span className="files-row-name">{t("filesLoadFailed")}</span>
          <button
            type="button"
            className="files-inline-btn"
            onClick={(event) => {
              event.stopPropagation();
              onRetry(path);
            }}
          >
            {t("filesRetry")}
          </button>
        </div>
      );
    }
    if (state.entries.length === 0) {
      return (
        <div className="files-row is-muted" style={{ paddingLeft: 8 + depth * 14 }}>
          <span className="files-chevron" />
          <span className="files-row-name">{t("filesEmptyDir")}</span>
        </div>
      );
    }
    return state.entries.map((entry) => {
      const isDir = entry.type === "dir";
      const status = changes?.byPath.get(entry.path);
      const count = isDir ? changes?.dirCount.get(entry.path) : undefined;
      const tone = entry.protected
        ? undefined
        : isDir
          ? changes?.dirStatus.get(entry.path)
          : status?.status;
      return (
        <Fragment key={entry.path}>
          <TreeRow
            type={entry.type}
            name={entry.name}
            depth={depth}
            expanded={entry.type === "dir" && open.has(entry.path)}
            loading={
              entry.type === "dir" &&
              open.has(entry.path) &&
              dirs[entry.path]?.status === "loading"
            }
            selected={selected === entry.path}
            protectedFile={entry.protected}
            ignored={entry.ignored}
            icon={
              entry.type === "dir" ? undefined : (
                <LanguageIcon name={entry.name} />
              )
            }
            badge={
              entry.type === "dir" && count ? (
                <span className="files-dir-count">{count}</span>
              ) : status && !entry.protected ? (
                <StatusBadge status={status.status} />
              ) : undefined
            }
            tone={tone}
            title={entry.path}
            meta={entry.type === "dir" ? undefined : formatSize(entry.size)}
            onClick={() => {
              if (entry.type === "dir") onToggle(entry.path);
              else onSelect(entry);
            }}
          />
          {entry.type === "dir" && open.has(entry.path)
            ? renderDir(entry.path, depth + 1)
            : null}
        </Fragment>
      );
    });
  };

  if (!root || (root.status === "loading" && root.entries.length === 0)) {
    return (
      <div className="files-tree" role="tree">
        <div className="files-row is-muted">
          <span className="files-row-name">{t("filesLoading")}</span>
        </div>
      </div>
    );
  }

  if (root.status === "error") {
    return (
      <div className="files-tree" role="tree">
        <div className="files-pane-error">
          <p>{root.error ?? t("filesLoadFailed")}</p>
          <button type="button" className="files-btn" onClick={() => onRetry("")}>
            {t("filesRetry")}
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="files-tree" role="tree" aria-label={t("filesTabFiles")}>
      {renderDir("", 0)}
    </div>
  );
}
