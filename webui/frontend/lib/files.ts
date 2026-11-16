import type { MessageKey } from "@/lib/i18n";
import type { ChangeEntry, ChangeStatus, DiffLine } from "@/lib/types";

/** Path helpers for POSIX-style workspace paths (never native separators). */

export function dirname(path: string): string {
  const index = path.lastIndexOf("/");
  return index === -1 ? "" : path.slice(0, index);
}

export function basename(path: string): string {
  const index = path.lastIndexOf("/");
  return index === -1 ? path : path.slice(index + 1);
}

export function joinPath(dir: string, name: string): string {
  return dir ? `${dir}/${name}` : name;
}

/** Ancestor directories of a path, shallow to deep, without the path itself. */
export function ancestorDirs(path: string): string[] {
  const result: string[] = [];
  let current = dirname(path);
  while (current) {
    result.unshift(current);
    current = dirname(current);
  }
  return result;
}

export function isSameOrUnder(path: string, dir: string): boolean {
  return dir === "" || path === dir || path.startsWith(`${dir}/`);
}

export function formatSize(size: number | null, unknown = "—"): string {
  if (size === null || !Number.isFinite(size)) return unknown;
  if (size < 1024) return `${size} B`;
  if (size < 1024 * 1024) return `${(size / 1024).toFixed(1)} KB`;
  if (size < 1024 * 1024 * 1024) {
    return `${(size / (1024 * 1024)).toFixed(1)} MB`;
  }
  return `${(size / (1024 * 1024 * 1024)).toFixed(1)} GB`;
}

export function statusLabel(status: ChangeStatus): string {
  switch (status) {
    case "added":
      return "A";
    case "deleted":
      return "D";
    case "renamed":
      return "R";
    case "copied":
      return "C";
    case "typechange":
      return "T";
    case "untracked":
      return "?";
    case "conflicted":
      return "U";
    default:
      return "M";
  }
}

export function statusKey(status: ChangeStatus): MessageKey {
  switch (status) {
    case "added":
      return "statusAdded";
    case "deleted":
      return "statusDeleted";
    case "renamed":
      return "statusRenamed";
    case "copied":
      return "statusCopied";
    case "typechange":
      return "statusTypechange";
    case "untracked":
      return "statusUntracked";
    case "conflicted":
      return "statusConflicted";
    default:
      return "statusModified";
  }
}

export interface ChangeNode {
  name: string;
  path: string;
  type: "dir" | "file";
  /** Changed files in this subtree, including the node itself. */
  count: number;
  entry?: ChangeEntry;
  children: ChangeNode[];
}

/**
 * Group the flat change list into a directory tree.
 *
 * Directories are created as needed and sorted before files; both are
 * ordered by name so the tree does not depend on git's output order.
 */
export function buildChangeTree(entries: ChangeEntry[]): ChangeNode[] {
  const root: ChangeNode = {
    name: "",
    path: "",
    type: "dir",
    count: 0,
    children: [],
  };
  const dirs = new Map<string, ChangeNode>([["", root]]);

  const ensureDir = (path: string): ChangeNode => {
    const existing = dirs.get(path);
    if (existing) return existing;
    const parent = ensureDir(dirname(path));
    const node: ChangeNode = {
      name: basename(path),
      path,
      type: "dir",
      count: 0,
      children: [],
    };
    parent.children.push(node);
    dirs.set(path, node);
    return node;
  };

  for (const entry of entries) {
    const parent = ensureDir(dirname(entry.path));
    const node: ChangeNode = {
      name: basename(entry.path),
      path: entry.path,
      type: "file",
      count: 1,
      entry,
      children: [],
    };
    parent.children.push(node);
  }

  const sort = (node: ChangeNode): number => {
    node.children.sort((left, right) => {
      if (left.type !== right.type) return left.type === "dir" ? -1 : 1;
      return left.name.localeCompare(right.name);
    });
    if (node.type === "file") return 1;
    node.count = node.children.reduce((total, child) => total + sort(child), 0);
    return node.count;
  };
  sort(root);
  return root.children;
}

export interface SplitRow {
  kind: "ctx" | "add" | "del" | "note";
  /** The deletion side of the row; null when this side is empty. */
  left: DiffLine | null;
  /** The addition side of the row. */
  right: DiffLine | null;
  /** Set for note rows, which span both sides. */
  note?: string;
}

/**
 * Pair a unified hunk's deletions and additions into side-by-side rows.
 *
 * Git emits deletions before additions, so the i-th deletion pairs with
 * the i-th addition; the longer side keeps its extra rows with an empty
 * cell on the other side. Context and note lines flush the pair buffer.
 */
export function toSplitRows(lines: DiffLine[]): SplitRow[] {
  const rows: SplitRow[] = [];
  let deletions: DiffLine[] = [];
  let additions: DiffLine[] = [];

  const flush = () => {
    const count = Math.max(deletions.length, additions.length);
    for (let index = 0; index < count; index += 1) {
      const left = deletions[index] ?? null;
      const right = additions[index] ?? null;
      rows.push({
        kind: left ? "del" : "add",
        left,
        right,
      });
    }
    deletions = [];
    additions = [];
  };

  for (const line of lines) {
    if (line.kind === "del") {
      deletions.push(line);
      continue;
    }
    if (line.kind === "add") {
      additions.push(line);
      continue;
    }
    flush();
    if (line.kind === "note") {
      rows.push({ kind: "note", left: null, right: null, note: line.text });
    } else {
      rows.push({ kind: "ctx", left: line, right: line });
    }
  }
  flush();
  return rows;
}

const CHANGE_RANK: Record<ChangeStatus, number> = {
  conflicted: 0,
  deleted: 1,
  modified: 2,
  typechange: 3,
  added: 4,
  renamed: 5,
  copied: 6,
  untracked: 7,
};

export interface ChangeIndex {
  byPath: Map<string, ChangeEntry>;
  /** Changed files below each directory, including nested ones. */
  dirCount: Map<string, number>;
  /** The most urgent status below each directory. */
  dirStatus: Map<string, ChangeStatus>;
}

/** Index a flat change list for the file tree's colours and counts. */
export function indexChanges(entries: ChangeEntry[]): ChangeIndex {
  const byPath = new Map<string, ChangeEntry>();
  const dirCount = new Map<string, number>();
  const dirStatus = new Map<string, ChangeStatus>();
  for (const entry of entries) {
    byPath.set(entry.path, entry);
    let dir = dirname(entry.path);
    while (dir) {
      dirCount.set(dir, (dirCount.get(dir) ?? 0) + 1);
      const previous = dirStatus.get(dir);
      if (
        previous === undefined ||
        CHANGE_RANK[entry.status] < CHANGE_RANK[previous]
      ) {
        dirStatus.set(dir, entry.status);
      }
      dir = dirname(dir);
    }
  }
  return { byPath, dirCount, dirStatus };
}

/** Every directory path in a change tree, for "expand all". */
export function changeTreeDirs(nodes: ChangeNode[]): string[] {
  const result: string[] = [];
  const walk = (items: ChangeNode[]) => {
    for (const node of items) {
      if (node.type !== "dir") continue;
      result.push(node.path);
      walk(node.children);
    }
  };
  walk(nodes);
  return result;
}
