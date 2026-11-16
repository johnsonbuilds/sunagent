"use client";

import { Fragment } from "react";

import LanguageIcon from "@/components/files/LanguageIcon";
import StatusBadge from "@/components/files/StatusBadge";
import TreeRow from "@/components/files/TreeRow";
import { useI18n } from "@/lib/i18n";
import type { ChangeNode } from "@/lib/files";
import type { ChangeEntry } from "@/lib/types";

function stats(entry: ChangeEntry): string {
  if (entry.binary) return "bin";
  const added = entry.additions ?? 0;
  const removed = entry.deletions ?? 0;
  if (added === 0 && removed === 0) return "";
  return `${added ? `+${added}` : ""}${added && removed ? " " : ""}${
    removed ? `−${removed}` : ""
  }`;
}

export default function ChangesTree({
  nodes,
  expanded,
  selected,
  onRetry,
  onToggle,
  onSelect,
}: {
  nodes: ChangeNode[];
  expanded: string[];
  selected: string | null;
  onRetry: () => void;
  onToggle: (path: string) => void;
  onSelect: (entry: ChangeEntry) => void;
}) {
  const { t } = useI18n();
  const open = new Set(expanded);

  const render = (items: ChangeNode[], depth: number) =>
    items.map((node) => {
      if (node.type === "dir") {
        const isOpen = open.has(node.path);
        return (
          <Fragment key={node.path}>
            <TreeRow
              type="dir"
              name={node.name}
              depth={depth}
              expanded={isOpen}
              title={node.path}
              badge={<span className="files-dir-count">{node.count}</span>}
              onClick={() => onToggle(node.path)}
            />
            {isOpen ? render(node.children, depth + 1) : null}
          </Fragment>
        );
      }
      const entry = node.entry;
      if (!entry) return null;
      return (
        <TreeRow
          key={node.path}
          type="file"
          name={node.name}
          depth={depth}
          selected={selected === entry.path}
          protectedFile={entry.protected}
          icon={<LanguageIcon name={node.name} />}
          title={entry.path}
          tone={entry.protected ? undefined : entry.status}
          badge={<StatusBadge status={entry.status} />}
          meta={stats(entry)}
          onClick={() => onSelect(entry)}
        />
      );
    });

  if (nodes.length === 0) {
    return (
      <div className="files-tree">
        <div className="files-pane-empty">
          <p>{t("filesNoChanges")}</p>
          <button type="button" className="files-btn" onClick={onRetry}>
            {t("filesRefresh")}
          </button>
        </div>
      </div>
    );
  }

  return (
    <div className="files-tree" role="tree" aria-label={t("filesTabChanges")}>
      {render(nodes, 0)}
    </div>
  );
}
