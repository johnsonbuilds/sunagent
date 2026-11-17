"use client";

import { useEffect, useState } from "react";

import { BackIcon, LockIcon } from "@/components/Icons";
import StatusBadge from "@/components/files/StatusBadge";
import { markRanges, markText } from "@/lib/diffMark";
import { statusKey, toSplitRows } from "@/lib/files";
import { highlightLanguage } from "@/lib/fileIcons";
import { highlightLine } from "@/lib/highlight";
import { useI18n } from "@/lib/i18n";
import type {
  ChangeEntry,
  DiffHunk,
  DiffLine,
  WorkspaceDiff,
} from "@/lib/types";
import { wordDiff, type DiffRange } from "@/lib/wordDiff";

export type DiffStyle = "unified" | "split";

const MARK = { add: "+", del: "−", ctx: " ", note: "\\" } as const;

/** Same ceiling as the async syntax highlighter. */
const MAX_DECORATED_LINES = 2000;

/** Token cells spent on intra-line word diffs per rendered change. */
const WORD_DIFF_BUDGET = 400_000;

/** Deleted and added line pairs of a hunk, in split-view order. */
function pairedLines(
  hunks: DiffHunk[],
): Array<{ left: DiffLine; right: DiffLine }> {
  const pairs: Array<{ left: DiffLine; right: DiffLine }> = [];
  for (const hunk of hunks) {
    for (const row of toSplitRows(hunk.lines)) {
      if (
        row.left &&
        row.right &&
        row.left !== row.right &&
        row.left.kind === "del" &&
        row.right.kind === "add"
      ) {
        pairs.push({ left: row.left, right: row.right });
      }
    }
  }
  return pairs;
}

/** Changed character ranges for every line that has a counterpart. */
function collectWordMarks(hunks: DiffHunk[]): Map<DiffLine, DiffRange[]> {
  const marks = new Map<DiffLine, DiffRange[]>();
  let budget = WORD_DIFF_BUDGET;
  for (const pair of pairedLines(hunks)) {
    if (budget <= 0) break;
    const outcome = wordDiff(pair.left.text, pair.right.text);
    budget -= outcome.cost;
    if (outcome.old && outcome.old.length > 0) {
      marks.set(pair.left, outcome.old);
    }
    if (outcome.new && outcome.new.length > 0) {
      marks.set(pair.right, outcome.new);
    }
  }
  return marks;
}

/** A diff line's rendered HTML: highlighted, word-marked, or both. */
function decoratedText(
  line: DiffLine,
  decorated: Map<DiffLine, string> | null,
): string | null {
  return decorated?.get(line) ?? null;
}

function UnifiedHunk({
  hunk,
  decorated,
}: {
  hunk: DiffHunk;
  decorated: Map<DiffLine, string> | null;
}) {
  return (
    <section className="diff-hunk">
      <div className="diff-hunk-head">{hunk.header}</div>
      {hunk.lines.map((line, index) => {
        const html = decoratedText(line, decorated);
        return (
          <div className={`diff-line kind-${line.kind}`} key={index}>
            {line.kind === "note" ? (
              <span className="diff-note">{line.text}</span>
            ) : (
              <>
                <span className="diff-no">{line.old ?? ""}</span>
                <span className="diff-no">{line.new ?? ""}</span>
                <span className="diff-mark">{MARK[line.kind]}</span>
                {html !== null ? (
                  <span
                    className="diff-text"
                    dangerouslySetInnerHTML={{ __html: html }}
                  />
                ) : (
                  <span className="diff-text">{line.text || "\u00a0"}</span>
                )}
              </>
            )}
          </div>
        );
      })}
    </section>
  );
}

function SplitSide({
  line,
  side,
  html,
}: {
  line: DiffLine | null;
  side: "old" | "new";
  html?: string;
}) {
  const number = side === "old" ? line?.old : line?.new;
  const tone = line ? line.kind : "empty";
  return (
    <>
      <span className={`diff-no kind-${tone}`}>{number ?? ""}</span>
      {html ? (
        <span
          className={`diff-cell kind-${tone}`}
          dangerouslySetInnerHTML={{ __html: html }}
        />
      ) : (
        <span className={`diff-cell kind-${tone}`}>
          {line ? line.text || "\u00a0" : ""}
        </span>
      )}
    </>
  );
}

function SplitHunk({
  hunk,
  decorated,
}: {
  hunk: DiffHunk;
  decorated: Map<DiffLine, string> | null;
}) {
  const rows = toSplitRows(hunk.lines);
  return (
    <section className="diff-hunk">
      <div className="diff-hunk-head">{hunk.header}</div>
      {rows.map((row, index) =>
        row.kind === "note" ? (
          <div className="diff-line kind-note" key={index}>
            <span className="diff-note">{row.note}</span>
          </div>
        ) : (
          <div className={`diff-split-row kind-${row.kind}`} key={index}>
            <SplitSide
              line={row.left}
              side="old"
              html={
                row.left
                  ? decoratedText(row.left, decorated) ?? undefined
                  : undefined
              }
            />
            <SplitSide
              line={row.right}
              side="new"
              html={
                row.right
                  ? decoratedText(row.right, decorated) ?? undefined
                  : undefined
              }
            />
          </div>
        ),
      )}
    </section>
  );
}

export default function DiffView({
  path,
  data,
  entry,
  style,
  loading,
  error,
  protectedPath,
  expanded,
  expanding,
  expandError,
  onStyleChange,
  onExpand,
  onCollapse,
  onRetry,
  onBack,
  onOpenInFiles,
}: {
  path: string | null;
  data: WorkspaceDiff | null;
  entry?: ChangeEntry | null;
  style: DiffStyle;
  loading: boolean;
  error: string | null;
  protectedPath?: string | null;
  expanded: boolean;
  expanding: boolean;
  expandError: string | null;
  onStyleChange: (style: DiffStyle) => void;
  onExpand: () => void;
  onCollapse: () => void;
  onRetry: () => void;
  onBack: () => void;
  onOpenInFiles: () => void;
}) {
  const { t } = useI18n();
  const language = path ? highlightLanguage(path) : null;
  const [decorated, setDecorated] = useState<Map<DiffLine, string> | null>(null);
  const totalLines =
    data?.hunks.reduce((count, hunk) => count + hunk.lines.length, 0) ?? 0;

  useEffect(() => {
    let cancelled = false;
    setDecorated(null);
    if (!data || data.binary || totalLines === 0) return;

    const canHighlight = language !== null && totalLines <= MAX_DECORATED_LINES;
    const marks =
      totalLines <= MAX_DECORATED_LINES
        ? collectWordMarks(data.hunks)
        : new Map<DiffLine, DiffRange[]>();

    void (async () => {
      const result = new Map<DiffLine, string>();
      for (const hunk of data.hunks) {
        for (const line of hunk.lines) {
          if (line.kind === "note" || !line.text) continue;
          let html: string | null = null;
          if (canHighlight && language) {
            html = await highlightLine(line.text, language);
            if (cancelled) return;
          }
          const ranges = marks.get(line);
          if (ranges && ranges.length > 0) {
            html = html
              ? markRanges(html, ranges)
              : markText(line.text, ranges);
          }
          if (html !== null) result.set(line, html);
        }
      }
      if (!cancelled) setDecorated(result);
    })();

    return () => {
      cancelled = true;
    };
  }, [data, language, totalLines]);

  if (!path) {
    return (
      <div className="files-empty">
        <h2>{t("filesNoChangeSelected")}</h2>
        <p>{t("filesNoChangeDescription")}</p>
      </div>
    );
  }

  const canExpand =
    data !== null &&
    !data.binary &&
    !protectedPath &&
    data.hunks.length > 0;
  const expandDisabled = data?.truncated === true;

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

        <div className="files-preview-actions">
          {canExpand ? (
            <div className="diff-style" role="group" aria-label="diff style">
              <button
                type="button"
                className={`diff-style-btn${style === "unified" ? " is-active" : ""}`}
                aria-pressed={style === "unified"}
                onClick={() => onStyleChange("unified")}
              >
                {t("filesDiffUnified")}
              </button>
              <button
                type="button"
                className={`diff-style-btn${style === "split" ? " is-active" : ""}`}
                aria-pressed={style === "split"}
                onClick={() => onStyleChange("split")}
              >
                {t("filesDiffSplit")}
              </button>
            </div>
          ) : null}
          {canExpand ? (
            <button
              type="button"
              className="files-btn"
              disabled={expanding || (!expanded && expandDisabled)}
              title={
                !expanded && expandDisabled
                  ? t("filesDiffTooLarge")
                  : undefined
              }
              onClick={expanded ? onCollapse : onExpand}
            >
              {expanded ? t("filesCollapseFull") : t("filesExpandFull")}
            </button>
          ) : null}
          {data && data.status !== "deleted" && !data.binary ? (
            <button type="button" className="files-btn" onClick={onOpenInFiles}>
              {t("filesOpenInFiles")}
            </button>
          ) : null}
        </div>
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
            {style === "split" ? (
              <div className="diff-split">
                {data?.hunks.map((hunk, index) => (
                  <SplitHunk hunk={hunk} decorated={decorated} key={index} />
                ))}
              </div>
            ) : (
              data?.hunks.map((hunk, index) => (
                <UnifiedHunk hunk={hunk} decorated={decorated} key={index} />
              ))
            )}
            {expandError ? (
              <div className="files-note is-warning">
                {t("filesDiffExpandFailed")}
              </div>
            ) : null}
            {canExpand ? (
              <div className="files-diff-foot">
                <span className="files-diff-hint">
                  {expanded ? t("filesDiffFullHint") : t("filesDiffCompactHint")}
                </span>
                <button
                  type="button"
                  className="files-btn"
                  disabled={expanding || (!expanded && expandDisabled)}
                  title={
                    !expanded && expandDisabled
                      ? t("filesDiffTooLarge")
                      : undefined
                  }
                  onClick={expanded ? onCollapse : onExpand}
                >
                  {expanded ? t("filesCollapseFull") : t("filesExpandFull")}
                </button>
              </div>
            ) : null}
            {data?.truncated ? (
              <div className="files-note">{t("filesDiffTruncated")}</div>
            ) : null}
          </>
        )}
      </div>
    </div>
  );
}
