"use client";

import { BackIcon, LockIcon } from "@/components/Icons";
import StatusBadge from "@/components/files/StatusBadge";
import { statusKey } from "@/lib/files";
import { useI18n } from "@/lib/i18n";
import type { ChangeEntry, WorkspaceDiff } from "@/lib/types";

const MARK = { add: "+", del: "−", ctx: " ", note: "\\" } as const;

export default function DiffView({
  path,
  data,
  entry,
  loading,
  error,
  protectedPath,
  onRetry,
  onBack,
  onOpenInFiles,
}: {
  path: string | null;
  data: WorkspaceDiff | null;
  entry?: ChangeEntry | null;
  loading: boolean;
  error: string | null;
  protectedPath?: string | null;
  onRetry: () => void;
  onBack: () => void;
  onOpenInFiles: () => void;
}) {
  const { t } = useI18n();

  if (!path) {
    return (
      <div className="files-empty">
        <h2>{t("filesNoChangeSelected")}</h2>
        <p>{t("filesNoChangeDescription")}</p>
      </div>
    );
  }

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
            {data ? (
              <>
                <StatusBadge
                  status={data.status}
                  title={t(statusKey(data.status))}
                />
                {entry?.staged ? (
                  <span className="files-tag">{t("filesStaged")}</span>
                ) : null}
                {entry?.unstaged ? (
                  <span className="files-tag">{t("filesUnstaged")}</span>
                ) : null}
                {data.old_path ? (
                  <span className="files-old-path">← {data.old_path}</span>
                ) : null}
              </>
            ) : null}
          </span>
        </div>
        {data && data.status !== "deleted" && !data.binary ? (
          <button type="button" className="files-btn" onClick={onOpenInFiles}>
            {t("filesOpenInFiles")}
          </button>
        ) : null}
      </header>

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
            <p>
              {/not readable from the web UI/i.test(error)
                ? t("filesProtected")
                : error}
            </p>
            <button type="button" className="files-btn" onClick={onRetry}>
              {t("filesRetry")}
            </button>
          </div>
        ) : data?.binary ? (
          <div className="files-pane-message">{t("filesDiffBinary")}</div>
        ) : (
          <>
            {data?.status === "conflicted" ? (
              <div className="files-note is-warning">{t("filesConflictDiff")}</div>
            ) : null}
            {data && data.hunks.length === 0 && !data.binary ? (
              <div className="files-pane-message">{t("filesNoTextDiff")}</div>
            ) : null}
            {data?.hunks.map((hunk, hunkIndex) => (
              <section className="diff-hunk" key={`${hunk.header}-${hunkIndex}`}>
                <div className="diff-hunk-head">{hunk.header}</div>
                {hunk.lines.map((line, lineIndex) => (
                  <div
                    className={`diff-line kind-${line.kind}`}
                    key={`${hunkIndex}-${lineIndex}`}
                  >
                    {line.kind === "note" ? (
                      <span className="diff-note">{line.text}</span>
                    ) : (
                      <>
                        <span className="diff-no">{line.old ?? ""}</span>
                        <span className="diff-no">{line.new ?? ""}</span>
                        <span className="diff-mark">{MARK[line.kind]}</span>
                        <span className="diff-text">{line.text || "\u00a0"}</span>
                      </>
                    )}
                  </div>
                ))}
              </section>
            ))}
            {data?.truncated ? (
              <div className="files-note">{t("filesDiffTruncated")}</div>
            ) : null}
          </>
        )}
      </div>
    </div>
  );
}
