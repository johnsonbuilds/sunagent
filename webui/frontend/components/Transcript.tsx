"use client";

import { useState, type RefObject } from "react";

import { render as renderMarkdown } from "@/lib/markdown";
import { useI18n, type MessageKey } from "@/lib/i18n";
import { useToast } from "@/lib/stores/toast";
import type { Segment, TextSegment, ToolSegment } from "@/lib/types";
import { CopyIcon, BranchIcon, EmptyMark } from "./Icons";

const SUGGESTIONS: MessageKey[] = [
  "suggestionFiles",
  "suggestionStructure",
  "suggestionTests",
  "suggestionReadme",
];

interface TranscriptProps {
  blocks: Segment[];
  messagesRef: RefObject<HTMLDivElement | null>;
  onScroll: () => void;
  onSuggestion: (text: string) => void;
  onBranch?: (segmentId: string) => void;
  branchDisabled?: boolean;
}

/** The run a segment belongs to (``{run_id}:{kind}:{n}``). */
function runIdOf(segmentId: string): string {
  return segmentId.split(":", 1)[0];
}

/**
 * Assistant blocks a branch may start from: the last block of its run, with
 * text in it.  Cutting anywhere else would keep a turn whose tool results
 * never arrived, so the server would refuse it too.
 */
function branchableIds(blocks: Segment[]): Set<string> {
  const lastOfRun = new Map<string, string>();
  for (const block of blocks) lastOfRun.set(runIdOf(block.id), block.id);
  const ids = new Set<string>();
  for (const block of blocks) {
    if (block.type !== "assistant") continue;
    if (!(block.content || "").trim()) continue;
    if (lastOfRun.get(runIdOf(block.id)) !== block.id) continue;
    ids.add(block.id);
  }
  return ids;
}

function formatValue(value: unknown): string {
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value);
  } catch {
    return String(value);
  }
}

function CopyButton({ content }: { content: string }) {
  const { t } = useI18n();
  const { showToast } = useToast();

  return (
    <button
      type="button"
      className="icon-btn medium"
      aria-label={t("msgCopy")}
      onClick={() => {
        navigator.clipboard.writeText(content);
        showToast(t("msgCopied"), "success");
      }}
    >
      <CopyIcon />
    </button>
  );
}

function BranchButton({ disabled, onClick }: {
  disabled: boolean;
  onClick: () => void;
}) {
  const { t } = useI18n();

  return (
    <button
      type="button"
      className="icon-btn medium"
      aria-label={t("msgBranch")}
      title={t("msgBranch")}
      disabled={disabled}
      onClick={onClick}
    >
      <BranchIcon />
    </button>
  );
}

function TextBlock({ block, onBranch, branchDisabled }: {
  block: TextSegment;
  onBranch?: () => void;
  branchDisabled?: boolean;
}) {
  if (block.type === "user") {
    return (
      <div className="msg-row" data-role="user">
        <div className="msg-bubble">{block.content || ""}</div>
        <div className="msg-actions">
          <CopyButton content={block.content || ""} />
        </div>
      </div>
    );
  }

  if (block.type === "reasoning") {
    return <ThinkingCard text={block.content || ""} />;
  }

  return (
      <div className="msg-row" data-role="assistant">
      <div className="msg-label">SunAgent</div>
      <div
        className="msg-body"
        dangerouslySetInnerHTML={{ __html: renderMarkdown(block.content || "") }}
      />
      <div className="msg-actions">
        {onBranch ? (
          <BranchButton
            disabled={Boolean(branchDisabled)}
            onClick={onBranch}
          />
        ) : null}
        <CopyButton content={block.content || ""} />
      </div>
    </div>
  );
}

function ThinkingCard({ text }: { text: string }) {
  const [open, setOpen] = useState(false);
  const { t } = useI18n();

  return (
    <div className={`thinking-card${open ? " open" : ""}`}>
      <button
        type="button"
        className="thinking-head"
        onClick={() => setOpen((value) => !value)}
      >
        <span>{t("thinking")}</span>
        <span className="thinking-caret">›</span>
      </button>
      <div className="thinking-body">
        <pre>{text}</pre>
      </div>
    </div>
  );
}

function ToolCard({ block }: { block: ToolSegment }) {
  const [open, setOpen] = useState(false);
  const { t } = useI18n();
  const status = block.status || "running";
  const args = block.arguments;
  const hasArgs =
    args && typeof args === "object" && Object.keys(args).length > 0;

  let meta = "";
  if (block.duration !== null && block.duration !== undefined) {
    meta += `${Number(block.duration).toFixed(2)}s`;
  }
  if (
    block.exit_code !== null &&
    block.exit_code !== undefined &&
    block.exit_code !== 0
  ) {
    meta += `${meta ? " · " : ""}${t("exitCode", {
      code: block.exit_code,
    })}`;
  }

  return (
    <div className={`tool-card${open ? " open" : ""}`} data-status={status}>
      <button
        type="button"
        className="tool-head"
        onClick={() => setOpen((value) => !value)}
      >
        <span className="tool-dot" />
        <span className="tool-name">{block.tool || t("toolFallback")}</span>
        <span className="tool-summary">{block.summary || ""}</span>
        <span className="tool-meta">{meta}</span>
        <span className="tool-caret">›</span>
      </button>
      <div className="tool-body">
        {hasArgs ? (
          <div className="tool-args">
            {Object.keys(args).map((key) => (
              <div className="tool-arg" key={key}>
                <span className="tool-arg-key">{key}</span>
                <span className="tool-arg-val">{formatValue(args[key])}</span>
              </div>
            ))}
          </div>
        ) : null}
        {block.error ? <div className="tool-error">{block.error}</div> : null}
        {block.output ? <pre className="tool-output">{block.output}</pre> : null}
      </div>
    </div>
  );
}

function EmptyState({ onSuggestion }: { onSuggestion: (text: string) => void }) {
  const { t } = useI18n();

  return (
    <div className="empty-state" id="emptyState">
      <EmptyMark />
      <h2>{t("emptyTitle")}</h2>
      <p>{t("emptyBody")}</p>
      <div className="suggestion-grid" id="suggestions">
        {SUGGESTIONS.map((key) => {
          const text = t(key);
          return (
            <button
              key={key}
              type="button"
              className="suggestion"
              onClick={() => onSuggestion(text)}
            >
              {text}
            </button>
          );
        })}
      </div>
    </div>
  );
}

export default function Transcript({
  blocks,
  messagesRef,
  onScroll,
  onSuggestion,
  onBranch,
  branchDisabled,
}: TranscriptProps) {
  const branchable = branchableIds(blocks);

  return (
    <div className="messages" id="messages" ref={messagesRef} onScroll={onScroll}>
      <div className="messages-inner" id="messagesInner">
        {blocks.length === 0 ? <EmptyState onSuggestion={onSuggestion} /> : null}
        {blocks.map((block) =>
          block.type === "tool" ? (
            <ToolCard key={block.id} block={block} />
          ) : (
            <TextBlock
              key={block.id}
              block={block}
              onBranch={
                onBranch && branchable.has(block.id)
                  ? () => onBranch(block.id)
                  : undefined
              }
              branchDisabled={branchDisabled}
            />
          ),
        )}
      </div>
    </div>
  );
}
