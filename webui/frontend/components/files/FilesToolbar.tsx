"use client";

import { RefreshIcon } from "@/components/Icons";
import { basename } from "@/lib/files";
import { useI18n } from "@/lib/i18n";

export default function FilesToolbar({
  root,
  tab,
  refreshing,
  includeIgnored,
  hiddenIgnored,
  onRefresh,
  onToggleIgnored,
  onExpandAll,
  onCollapseAll,
}: {
  root: string | null;
  tab: "files" | "changes";
  refreshing: boolean;
  includeIgnored: boolean;
  hiddenIgnored: number;
  onRefresh: () => void;
  onToggleIgnored: () => void;
  onExpandAll: () => void;
  onCollapseAll: () => void;
}) {
  const { t } = useI18n();
  const label = root ? basename(root) || root : t("filesWorkspaceRoot");

  return (
    <div className="files-actions">
      <span className="files-root" title={root ?? undefined}>
        {label}
      </span>
      {tab === "files" ? (
        <button
          type="button"
          className={`files-btn files-ignored-toggle${
            includeIgnored ? " is-on" : ""
          }`}
          onClick={onToggleIgnored}
          title={
            includeIgnored ? t("filesHideIgnored") : t("filesShowIgnored")
          }
        >
          {includeIgnored ? t("filesHideIgnored") : t("filesShowIgnored")}
          {!includeIgnored && hiddenIgnored > 0 ? (
            <span className="files-hidden-count">
              {t("filesHiddenIgnored", { count: hiddenIgnored })}
            </span>
          ) : null}
        </button>
      ) : (
        <>
          <button type="button" className="files-btn" onClick={onExpandAll}>
            {t("filesExpandAll")}
          </button>
          <button type="button" className="files-btn" onClick={onCollapseAll}>
            {t("filesCollapseAll")}
          </button>
        </>
      )}
      <button
        type="button"
        className={`files-icon-btn${refreshing ? " is-busy" : ""}`}
        title={t("filesRefresh")}
        aria-label={t("filesRefresh")}
        onClick={onRefresh}
      >
        <RefreshIcon />
      </button>
    </div>
  );
}
