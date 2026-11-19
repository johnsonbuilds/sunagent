import type { MessageKey } from "@/lib/i18n";
import type { ChangeEntry, ChangeStatus, DiffLine } from "@/lib/types";

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

export function toSplitRows(lines: DiffLine[]): SplitRow[] {
  const rows: SplitRow[] = [];
  let deletions: DiffLine[] = [];
  let additions: DiffLine[] = [];

  const flush = () => {
    rows.push(...alignBlock(deletions, additions));
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

/** Below this share of shared characters two lines are not shown side by side. */
const MIN_PAIR_SIMILARITY = 0.3;

/** Largest deletion-times-addition table a block may align with. */
const MAX_ALIGN_CELLS = 2500;

/** Share of the longer line taken by its common prefix and suffix. */
function lineSimilarity(left: string, right: string): number {
  if (left === right) return 1;
  const longest = Math.max(left.length, right.length);
  if (longest === 0) return 1;
  const shortest = Math.min(left.length, right.length);
  let prefix = 0;
  while (prefix < shortest && left[prefix] === right[prefix]) prefix += 1;
  let suffix = 0;
  while (
    suffix < shortest - prefix &&
    left[left.length - 1 - suffix] === right[right.length - 1 - suffix]
  ) {
    suffix += 1;
  }
  return (prefix + suffix) / longest;
}

function pairByIndex(deletions: DiffLine[], additions: DiffLine[]): SplitRow[] {
  const rows: SplitRow[] = [];
  const count = Math.max(deletions.length, additions.length);
  for (let index = 0; index < count; index += 1) {
    const left = deletions[index] ?? null;
    const right = additions[index] ?? null;
    rows.push({ kind: left ? "del" : "add", left, right });
  }
  return rows;
}

function alignBlock(
  deletions: DiffLine[],
  additions: DiffLine[],
): SplitRow[] {
  const n = deletions.length;
  const m = additions.length;
  if (n === 0 || m === 0) return pairByIndex(deletions, additions);
  if (n * m > MAX_ALIGN_CELLS) return pairByIndex(deletions, additions);

  // score[i][j] = best similarity sum for deletions[i:] and additions[j:].
  const width = m + 1;
  const score = new Float64Array((n + 1) * width);
  const similarity = new Float64Array(n * m);
  for (let i = n - 1; i >= 0; i -= 1) {
    const row = i * width;
    for (let j = m - 1; j >= 0; j -= 1) {
      const alike = lineSimilarity(deletions[i].text, additions[j].text);
      similarity[i * m + j] = alike;
      let best = Math.max(score[row + width + j], score[row + j + 1]);
      if (alike >= MIN_PAIR_SIMILARITY) {
        best = Math.max(best, score[row + width + j + 1] + alike);
      }
      score[row + j] = best;
    }
  }

  const rows: SplitRow[] = [];
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    const row = i * width;
    const alike = similarity[i * m + j];
    if (
      alike >= MIN_PAIR_SIMILARITY &&
      score[row + j] === score[row + width + j + 1] + alike
    ) {
      rows.push({ kind: "del", left: deletions[i], right: additions[j] });
      i += 1;
      j += 1;
    } else if (score[row + width + j] >= score[row + j + 1]) {
      rows.push({ kind: "del", left: deletions[i], right: null });
      i += 1;
    } else {
      rows.push({ kind: "add", left: null, right: additions[j] });
      j += 1;
    }
  }
  while (i < n) rows.push({ kind: "del", left: deletions[i++], right: null });
  while (j < m) rows.push({ kind: "add", left: null, right: additions[j++] });
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
