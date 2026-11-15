"use client";

import { useI18n } from "@/lib/i18n";

export type FilesTabId = "files" | "changes";

export default function FilesTabs({
  tab,
  changes,
  onChange,
}: {
  tab: FilesTabId;
  changes: number;
  onChange: (tab: FilesTabId) => void;
}) {
  const { t } = useI18n();
  return (
    <div className="files-tabs" role="tablist" aria-label={t("navFiles")}>
      <button
        type="button"
        role="tab"
        aria-selected={tab === "files"}
        className={`files-tab${tab === "files" ? " is-active" : ""}`}
        onClick={() => onChange("files")}
      >
        {t("filesTabFiles")}
      </button>
      <button
        type="button"
        role="tab"
        aria-selected={tab === "changes"}
        className={`files-tab${tab === "changes" ? " is-active" : ""}`}
        onClick={() => onChange("changes")}
      >
        {t("filesTabChanges")}
        {changes > 0 ? <span className="files-tab-count">{changes}</span> : null}
      </button>
    </div>
  );
}
