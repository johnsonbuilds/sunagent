"use client";

import type { ReactNode } from "react";

import {
  ChevronIcon,
  FileIcon,
  FolderIcon,
  FolderOpenIcon,
  LockIcon,
} from "@/components/Icons";
import type { ChangeStatus } from "@/lib/types";

export default function TreeRow({
  type,
  name,
  depth,
  expanded = false,
  loading = false,
  selected = false,
  protectedFile = false,
  ignored = false,
  icon,
  badge,
  meta,
  tone,
  title,
  onClick,
}: {
  type: "dir" | "file" | "symlink";
  name: string;
  depth: number;
  expanded?: boolean;
  loading?: boolean;
  selected?: boolean;
  protectedFile?: boolean;
  ignored?: boolean;
  icon?: ReactNode;
  badge?: ReactNode;
  meta?: ReactNode;
  tone?: ChangeStatus;
  title?: string;
  onClick: () => void;
}) {
  const isDir = type === "dir";
  const Icon = isDir ? (expanded ? FolderOpenIcon : FolderIcon) : FileIcon;
  return (
    <button
      type="button"
      role="treeitem"
      aria-expanded={isDir ? expanded : undefined}
      aria-selected={selected}
      className={`files-row${selected ? " is-selected" : ""}${
        protectedFile ? " is-protected" : ""
      }${tone ? ` is-status-${tone}` : ""}${ignored ? " is-ignored" : ""}`}
      style={{ paddingLeft: 8 + depth * 14 }}
      title={title ?? name}
      onClick={onClick}
    >
      <span className={`files-chevron${expanded ? " is-open" : ""}`}>
        {isDir ? <ChevronIcon /> : null}
      </span>
      <span className="files-row-icon">
        {icon ?? <Icon />}
        {loading ? <span className="files-spinner" /> : null}
      </span>
      <span className="files-row-name">{name}</span>
      {protectedFile ? (
        <span className="files-row-lock" title="protected">
          <LockIcon />
        </span>
      ) : null}
      {badge}
      {meta ? <span className="files-row-meta">{meta}</span> : null}
    </button>
  );
}
