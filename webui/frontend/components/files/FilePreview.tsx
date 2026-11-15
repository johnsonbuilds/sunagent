"use client";

import { useState } from "react";

import { BackIcon, CopyIcon, LockIcon } from "@/components/Icons";
import { formatSize } from "@/lib/files";
import { useI18n } from "@/lib/i18n";
import type { WorkspaceFile } from "@/lib/types";

export default function FilePreview({
  path,
  data,
  loading,
  error,
  changedOnDisk,
  protectedPath,
  onBack,
  onRetry,
}: {
  path: string | null;
  data: WorkspaceFile | null;
  loading: boolean;
  error: string | null;
  changedOnDisk: boolean;
  protectedPath?: string | null;
  onBack: () => void;
  onRetry: () => void;
}) {
  const { t } = useI18n();
  const [copied, setCopied] = useState(false);

  if (!path) {
    return (
      <div className="files-empty">
        <h2>{t("filesSelectTitle")}</h2>
        <p>{t("filesSelectDescription")}</p>
      </div>
    );
  }

  const copy = async () => {
    try {
      await navigator.clipboard.writeText(path);
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1200);
    } catch {
      /* clipboard may be unavailable on http origins */
    }
  };

  const lines = (data?.content ?? "")
    .split("\n")
    .map((line) => (line.endsWith("\r") ? line.slice(0, -1) : line));
  if (lines.length > 0 && lines[lines.length - 1] === "") lines.pop();

  return (
    <div className="files-preview">
      <header className="files-preview-head">
        <button
          type="button"
          className="files-icon-btn files-back-btn"
          title={t("filesBack")}
          onClick={onBack}
        >
          <BackIcon />
        </button>
        <div className="files-preview-title">
          <span className="files-path">{path}</span>
          <span className="files-preview-meta">
            {data ? formatSize(data.size) : ""}
            {data && data.lines !== null ? ` · ${t("filesLineCount", { count: data.lines })}` : ""}
          </span>
        </div>
        <button
          type="button"
          className="files-icon-btn"
          title={copied ? t("filesCopied") : t("filesCopyPath")}
          onClick={() => void copy()}
        >
          <CopyIcon />
        </button>
      </header>

      {changedOnDisk ? (
        <div className="files-note">{t("filesChangedOnDisk")}</div>
      ) : null}

      <div className="files-preview-body">
        {protectedPath ? (
          <div className="files-pane-message">
            <span className="files-lock-mark">
              <LockIcon />
            </span>
            <p>{t("filesProtected")}</p>
          </div>
        ) : loading && !data ? (
          <div className="files-pane-message">{t("filesLoading")}</div>
        ) : error ? (
          <div className="files-pane-error">
            <p>{error}</p>
            <button type="button" className="files-btn" onClick={onRetry}>
              {t("filesRetry")}
            </button>
          </div>
        ) : data?.binary ? (
          <div className="files-pane-message">
            <span className="files-lock-mark">
              <LockIcon />
            </span>
            <p>{t("filesBinary", { size: formatSize(data.size) })}</p>
          </div>
        ) : lines.length === 0 ? (
          <div className="files-pane-message">{t("filesEmptyFile")}</div>
        ) : (
          <div className="files-code" role="presentation">
            {lines.map((line, index) => (
              <div className="files-code-line" key={index}>
                <span className="files-code-no">{index + 1}</span>
                <span className="files-code-text">{line || "\u00a0"}</span>
              </div>
            ))}
          </div>
        )}
        {data?.truncated ? (
          <div className="files-note">{t("filesTruncated")}</div>
        ) : null}
      </div>
    </div>
  );
}
