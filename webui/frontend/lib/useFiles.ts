"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { api } from "@/lib/api";
import type {
  WorkspaceChanges,
  WorkspaceDiff,
  WorkspaceEntry,
  WorkspaceFile,
  WorkspaceTree,
} from "@/lib/types";

function errorMessage(error: unknown): string {
  return error instanceof Error && error.message ? error.message : "request failed";
}

export interface DirState {
  status: "idle" | "loading" | "ready" | "error";
  entries: WorkspaceEntry[];
  hiddenIgnored: number;
  truncated: boolean;
  error: string | null;
}

function treeUrl(path: string, includeIgnored: boolean): string {
  const query = new URLSearchParams({ path });
  if (includeIgnored) query.set("ignored", "true");
  return `/api/workspace/tree?${query.toString()}`;
}

export interface FileTreeState {
  dirs: Record<string, DirState>;
  expanded: string[];
  loadDir: (path: string, force?: boolean) => Promise<WorkspaceEntry[] | null>;
  toggleDir: (path: string) => void;
  expandTo: (path: string) => Promise<void>;
  expandAll: (paths: string[]) => Promise<void>;
  collapseAll: () => void;
  refreshVisible: () => Promise<void>;
}

/**
 * One directory arrives per request and stays cached; expanding a
 * directory that already has entries does not fetch again.  Changing the
 * "show ignored" switch resets the cache, because every listing changes.
 */
export function useFileTree(includeIgnored: boolean): FileTreeState {
  const [dirs, setDirs] = useState<Record<string, DirState>>({});
  const [expanded, setExpanded] = useState<string[]>([]);
  const dirsRef = useRef(dirs);
  const expandedRef = useRef(expanded);
  dirsRef.current = dirs;
  expandedRef.current = expanded;

  const includeRef = useRef(includeIgnored);
  includeRef.current = includeIgnored;
  const requests = useRef(new Map<string, AbortController>());
  const inflight = useRef(new Map<string, Promise<WorkspaceEntry[] | null>>());
  const epochRef = useRef(0);

  const writeDir = useCallback((path: string, next: DirState) => {
    setDirs((current) => ({ ...current, [path]: next }));
  }, []);

  const loadDir = useCallback(
    (path: string, force = false): Promise<WorkspaceEntry[] | null> => {
      const cached = dirsRef.current[path];
      if (!force && cached?.status === "ready") {
        return Promise.resolve(cached.entries);
      }
      if (!force) {
        // A second caller (a deep link racing the initial root load) must
        // wait for the same request instead of treating "loading" as an
        // empty directory.
        const pending = inflight.current.get(path);
        if (pending) return pending;
      }

      requests.current.get(path)?.abort();
      const controller = new AbortController();
      requests.current.set(path, controller);
      const epoch = epochRef.current;

      writeDir(path, {
        status: "loading",
        entries: cached?.entries ?? [],
        hiddenIgnored: cached?.hiddenIgnored ?? 0,
        truncated: cached?.truncated ?? false,
        error: null,
      });

      const promise = api<WorkspaceTree>(treeUrl(path, includeRef.current), {
        signal: controller.signal,
      })
        .then((data) => {
          if (epoch !== epochRef.current) return null;
          writeDir(path, {
            status: "ready",
            entries: data.entries,
            hiddenIgnored: data.hidden_ignored,
            truncated: data.truncated,
            error: null,
          });
          return data.entries;
        })
        .catch((error) => {
          if (controller.signal.aborted || epoch !== epochRef.current) {
            return null;
          }
          writeDir(path, {
            status: "error",
            entries: cached?.entries ?? [],
            hiddenIgnored: cached?.hiddenIgnored ?? 0,
            truncated: cached?.truncated ?? false,
            error: errorMessage(error),
          });
          return null;
        })
        .finally(() => {
          if (requests.current.get(path) === controller) {
            requests.current.delete(path);
          }
          if (inflight.current.get(path) === promise) {
            inflight.current.delete(path);
          }
        });

      inflight.current.set(path, promise);
      return promise;
    },
    [writeDir],
  );

  useEffect(() => {
    requests.current.forEach((controller) => controller.abort());
    requests.current.clear();
    epochRef.current += 1;
    setDirs({});
    setExpanded([]);
    void loadDir("", true);
    return () => {
      requests.current.forEach((controller) => controller.abort());
      requests.current.clear();
    };
  }, [includeIgnored, loadDir]);

  const toggleDir = useCallback(
    (path: string) => {
      const open = expandedRef.current.includes(path);
      setExpanded((current) =>
        open ? current.filter((item) => item !== path) : [...current, path],
      );
      if (!open) void loadDir(path);
    },
    [loadDir],
  );

  const expandTo = useCallback(
    async (path: string) => {
      const chain: string[] = [];
      let current = path;
      while (current) {
        chain.unshift(current);
        current = current.slice(0, Math.max(0, current.lastIndexOf("/")));
      }
      chain.unshift("");
      for (const dir of chain) {
        await loadDir(dir);
      }
      setExpanded((existing) => {
        const merged = new Set(existing);
        for (const dir of chain) if (dir) merged.add(dir);
        return [...merged];
      });
    },
    [loadDir],
  );

  const expandAll = useCallback(
    async (paths: string[]) => {
      await Promise.all(paths.map((path) => loadDir(path)));
      setExpanded((existing) => [...new Set([...existing, ...paths])]);
    },
    [loadDir],
  );

  const collapseAll = useCallback(() => setExpanded([]), []);

  const refreshVisible = useCallback(async () => {
    const paths = new Set([""]);
    for (const path of expandedRef.current) paths.add(path);
    await Promise.all([...paths].map((path) => loadDir(path, true)));
  }, [loadDir]);

  return {
    dirs,
    expanded,
    loadDir,
    toggleDir,
    expandTo,
    expandAll,
    collapseAll,
    refreshVisible,
  };
}

export interface ChangesState {
  data: WorkspaceChanges | null;
  loading: boolean;
  error: string | null;
  updatedAt: number | null;
  refresh: (quiet?: boolean) => Promise<void>;
}

export function useChanges(): ChangesState {
  const [data, setData] = useState<WorkspaceChanges | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [updatedAt, setUpdatedAt] = useState<number | null>(null);
  const request = useRef<AbortController | null>(null);

  const load = useCallback(async (quiet = false) => {
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    if (!quiet) setLoading(true);
    try {
      const next = await api<WorkspaceChanges>("/api/workspace/changes", {
        signal: controller.signal,
      });
      setData(next);
      setUpdatedAt(Date.now());
      setError(null);
    } catch (caught) {
      if (!controller.signal.aborted) setError(errorMessage(caught));
    } finally {
      if (request.current === controller) {
        request.current = null;
        setLoading(false);
      }
    }
  }, []);

  useEffect(() => {
    void load();
    return () => request.current?.abort();
  }, [load]);

  return { data, loading, error, updatedAt, refresh: load };
}

export interface FilePreviewState {
  data: WorkspaceFile | null;
  loading: boolean;
  error: string | null;
  changedOnDisk: boolean;
}

export function useFilePreview(
  path: string | null,
  version: number,
): FilePreviewState {
  const [data, setData] = useState<WorkspaceFile | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [changedOnDisk, setChanged] = useState(false);
  const previous = useRef<WorkspaceFile | null>(null);

  useEffect(() => {
    if (!path) {
      previous.current = null;
      setData(null);
      setError(null);
      setLoading(false);
      setChanged(false);
      return;
    }
    const controller = new AbortController();
    setLoading(true);
    setError(null);
    // A different file should not flash the previous file's text under
    // the new header; a refresh of the same file keeps it visible.
    setData((current) => (current && current.path === path ? current : null));

    api<WorkspaceFile>(
      `/api/workspace/file?path=${encodeURIComponent(path)}`,
      { signal: controller.signal },
    )
      .then((next) => {
        const before = previous.current;
        setChanged(
          before !== null &&
            before.path === next.path &&
            (before.mtime !== next.mtime || before.size !== next.size),
        );
        previous.current = next;
        setData(next);
        setLoading(false);
      })
      .catch((caught) => {
        if (controller.signal.aborted) return;
        setError(errorMessage(caught));
        setLoading(false);
      });

    return () => controller.abort();
  }, [path, version]);

  return { data, loading, error, changedOnDisk };
}

export interface FileDiffState {
  /** The compact diff, or the expanded one while `expanded` is true. */
  data: WorkspaceDiff | null;
  loading: boolean;
  error: string | null;
  expanded: boolean;
  expanding: boolean;
  expandError: string | null;
  expand: () => void;
  collapse: () => void;
}

/**
 * A compact diff loads with the selection.  Expanding fetches a second,
 * full-context diff once and keeps it until the file or refresh changes;
 * collapsing returns to the cached compact diff without a request.
 */
export function useFileDiff(
  path: string | null,
  oldPath: string | null,
  version: number,
): FileDiffState {
  const [compact, setCompact] = useState<WorkspaceDiff | null>(null);
  const [full, setFull] = useState<WorkspaceDiff | null>(null);
  const [expanded, setExpanded] = useState(false);
  const [loading, setLoading] = useState(false);
  const [expanding, setExpanding] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [expandError, setExpandError] = useState<string | null>(null);
  const fullRequest = useRef<AbortController | null>(null);

  useEffect(() => {
    fullRequest.current?.abort();
    setFull(null);
    setExpanded(false);
    setExpandError(null);
    if (!path) {
      setCompact(null);
      setError(null);
      setLoading(false);
      return;
    }
    const controller = new AbortController();
    const query = new URLSearchParams({ path });
    if (oldPath) query.set("old_path", oldPath);
    setLoading(true);
    setError(null);
    setCompact((current) => (current && current.path === path ? current : null));

    api<WorkspaceDiff>(`/api/workspace/diff?${query.toString()}`, {
      signal: controller.signal,
    })
      .then((next) => {
        setCompact(next);
        setLoading(false);
      })
      .catch((caught) => {
        if (controller.signal.aborted) return;
        setError(errorMessage(caught));
        setLoading(false);
      });

    return () => controller.abort();
  }, [path, oldPath, version]);

  const expand = useCallback(() => {
    if (!path || expanded) return;
    if (full) {
      setExpanded(true);
      return;
    }
    fullRequest.current?.abort();
    const controller = new AbortController();
    fullRequest.current = controller;
    const query = new URLSearchParams({ path, full: "true" });
    if (oldPath) query.set("old_path", oldPath);
    setExpanding(true);
    setExpandError(null);

    api<WorkspaceDiff>(`/api/workspace/diff?${query.toString()}`, {
      signal: controller.signal,
    })
      .then((next) => {
        setFull(next);
        setExpanded(true);
        setExpanding(false);
      })
      .catch((caught) => {
        if (controller.signal.aborted) return;
        setExpandError(errorMessage(caught));
        setExpanding(false);
      });
  }, [path, oldPath, expanded, full]);

  const collapse = useCallback(() => setExpanded(false), []);

  return {
    data: expanded && full ? full : compact,
    loading,
    error,
    expanded,
    expanding,
    expandError,
    expand,
    collapse,
  };
}

