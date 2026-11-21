"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import { api } from "@/lib/api";
import type {
  BoardOverview,
  EvalBenchmarks,
  EvalDiagnosisRow,
  EvalOverview,
  EvalRunDetail,
  EvalTaskDetail,
  EvalTaskPage,
} from "@/lib/types";

export interface EvalFilters {
  range: string;
  origin: string;
  harness: string;
  model: string;
}

export interface TaskQuery {
  q: string;
  status: string;
  sort: string;
  order: "asc" | "desc";
  page: number;
  page_size: number;
}

export const DEFAULT_FILTERS: EvalFilters = {
  range: "7d",
  origin: "",
  harness: "",
  model: "",
};

const DEFAULT_TASK_QUERY: TaskQuery = {
  q: "",
  status: "",
  sort: "updated_at",
  order: "desc",
  page: 1,
  page_size: 25,
};

// The search box refetches on every keystroke without this.
const TASK_DEBOUNCE_MS = 250;

function filterQuery(filters: EvalFilters): string {
  const params = new URLSearchParams({ range: filters.range });
  if (filters.origin) params.set("origin", filters.origin);
  if (filters.harness) params.set("harness", filters.harness);
  if (filters.model) params.set("model", filters.model);
  return params.toString();
}

/** ``board-overview`` only accepts a time window; the other slices are run-level. */
function rangeQuery(filters: EvalFilters): string {
  return new URLSearchParams({ range: filters.range }).toString();
}

function buildTaskQuery(filters: EvalFilters, query: TaskQuery): string {
  const params = new URLSearchParams(filterQuery(filters));
  if (query.q.trim()) params.set("q", query.q.trim());
  if (query.status) params.set("status", query.status);
  params.set("sort", query.sort);
  params.set("order", query.order);
  params.set("page", String(query.page));
  params.set("page_size", String(query.page_size));
  return params.toString();
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

/**
 * Filter options that survive a narrowing selection.
 *
 * ``observed`` are the slice keys of the current response: once a harness or
 * model is selected the slice only describes that one value, so previously
 * seen names (plus the configured ones) are kept for the dropdown.
 */
export function useStickyOptions(
  observed: string[],
  configured: string[],
): string[] {
  const observedKey = observed.join("\u0000");
  const configuredKey = configured.join("\u0000");
  const [seen, setSeen] = useState<string[]>([]);

  useEffect(() => {
    const names = observedKey ? observedKey.split("\u0000") : [];
    if (!names.length) return;
    setSeen((current) => {
      const missing = names.filter((name) => !current.includes(name));
      return missing.length ? [...current, ...missing] : current;
    });
  }, [observedKey]);

  return useMemo(() => {
    const names = new Set<string>(
      configuredKey ? configuredKey.split("\u0000") : [],
    );
    for (const name of seen) names.add(name);
    return [...names].filter(Boolean).sort();
  }, [configuredKey, seen]);
}

export interface EvaluationState {
  filters: EvalFilters;
  setFilters: (patch: Partial<EvalFilters>) => void;
  taskQuery: TaskQuery;
  setTaskQuery: (patch: Partial<TaskQuery>) => void;
  overview: EvalOverview | null;
  board: BoardOverview | null;
  tasks: EvalTaskPage | null;
  benchmarks: EvalBenchmarks | null;
  diagnoses: EvalDiagnosisRow[];
  loading: boolean;
  tasksLoading: boolean;
  error: string | null;
  refresh: () => void;
  detail: EvalTaskDetail | null;
  detailId: string | null;
  detailLoading: boolean;
  /** Open the drawer, optionally jumping straight into one run's digest. */
  openTask: (taskId: string, initialRunId?: string) => Promise<void>;
  closeTask: () => void;
  run: EvalRunDetail | null;
  runId: string | null;
  runLoading: boolean;
  openRun: (runId: string) => Promise<void>;
  closeRun: () => void;
}

/**
 * One hook owns every dataset of the evaluation page.
 *
 * ``overview`` / ``board-overview`` follow the filter bar immediately, the
 * task list is debounced on its own key (search, paging, sorting), the
 * benchmark and diagnosis lists are fetched once per refresh, and the two
 * drawers load on demand.  Responses are dropped when a newer request has
 * already been issued, so fast filter switches cannot show stale numbers.
 */
export function useEvaluation(): EvaluationState {
  const [filters, setFiltersState] = useState<EvalFilters>(DEFAULT_FILTERS);
  const [taskQuery, setTaskQueryState] = useState<TaskQuery>(DEFAULT_TASK_QUERY);
  const [overview, setOverview] = useState<EvalOverview | null>(null);
  const [board, setBoard] = useState<BoardOverview | null>(null);
  const [tasks, setTasks] = useState<EvalTaskPage | null>(null);
  const [benchmarks, setBenchmarks] = useState<EvalBenchmarks | null>(null);
  const [diagnoses, setDiagnoses] = useState<EvalDiagnosisRow[]>([]);
  const [loading, setLoading] = useState(true);
  const [tasksLoading, setTasksLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [seq, setSeq] = useState(0);
  const [detail, setDetail] = useState<EvalTaskDetail | null>(null);
  const [detailId, setDetailId] = useState<string | null>(null);
  const [detailLoading, setDetailLoading] = useState(false);
  const [run, setRun] = useState<EvalRunDetail | null>(null);
  const [runId, setRunId] = useState<string | null>(null);
  const [runLoading, setRunLoading] = useState(false);

  const mainToken = useRef(0);
  const taskToken = useRef(0);
  const detailToken = useRef(0);
  const runToken = useRef(0);

  const setFilters = useCallback((patch: Partial<EvalFilters>) => {
    setFiltersState((current) => ({ ...current, ...patch }));
    // A new slice widens or narrows the population: start from page one.
    setTaskQueryState((current) => ({ ...current, page: 1 }));
  }, []);

  const setTaskQuery = useCallback((patch: Partial<TaskQuery>) => {
    setTaskQueryState((current) => {
      const next = { ...current, ...patch };
      if (patch.q !== undefined || patch.status !== undefined
          || patch.sort !== undefined || patch.order !== undefined) {
        next.page = patch.page ?? 1;
      }
      return next;
    });
  }, []);

  const refresh = useCallback(() => setSeq((value) => value + 1), []);

  const filtersKey = filterQuery(filters);
  const rangeKey = rangeQuery(filters);
  const tasksKey = buildTaskQuery(filters, taskQuery);

  useEffect(() => {
    const token = ++mainToken.current;
    setLoading(true);
    void (async () => {
      try {
        const [nextOverview, nextBoard] = await Promise.all([
          api<EvalOverview>(`/api/evaluation/overview?${filtersKey}`),
          api<BoardOverview>(`/api/evaluation/board-overview?${rangeKey}`),
        ]);
        if (token !== mainToken.current) return;
        setOverview(nextOverview);
        setBoard(nextBoard);
        setError(null);
      } catch (failure) {
        if (token !== mainToken.current) return;
        setError(errorMessage(failure));
      } finally {
        if (token === mainToken.current) setLoading(false);
      }
    })();
  }, [filtersKey, rangeKey, seq]);

  useEffect(() => {
    let timer: ReturnType<typeof setTimeout> | undefined;
    const token = ++taskToken.current;
    setTasksLoading(true);
    timer = setTimeout(() => {
      void (async () => {
        try {
          const nextTasks = await api<EvalTaskPage>(
            `/api/evaluation/tasks?${tasksKey}`,
          );
          if (token !== taskToken.current) return;
          setTasks(nextTasks);
        } catch (failure) {
          if (token !== taskToken.current) return;
          setError(errorMessage(failure));
        } finally {
          if (token === taskToken.current) setTasksLoading(false);
        }
      })();
    }, TASK_DEBOUNCE_MS);
    return () => {
      if (timer) clearTimeout(timer);
    };
  }, [tasksKey, seq]);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const [nextBenchmarks, nextDiagnoses] = await Promise.all([
          api<EvalBenchmarks>("/api/evaluation/benchmarks"),
          api<{ sessions: EvalDiagnosisRow[] }>("/api/evaluation/diagnoses"),
        ]);
        if (cancelled) return;
        setBenchmarks(nextBenchmarks);
        setDiagnoses(nextDiagnoses.sessions ?? []);
      } catch {
        // Both lists are supplementary: a failure here must not blank the
        // KPIs that already loaded.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [seq]);

  const openRun = useCallback(async (id: string) => {
    const token = ++runToken.current;
    setRunId(id);
    setRunLoading(true);
    try {
      const next = await api<EvalRunDetail>(
        `/api/evaluation/runs/${encodeURIComponent(id)}`,
      );
      if (token !== runToken.current) return;
      setRun(next);
    } catch {
      if (token === runToken.current) setRun(null);
    } finally {
      if (token === runToken.current) setRunLoading(false);
    }
  }, []);

  const closeRun = useCallback(() => {
    runToken.current += 1;
    setRun(null);
    setRunId(null);
    setRunLoading(false);
  }, []);

  // Any drawer change invalidates in-flight run requests, otherwise a slow
  // digest could land inside a different task's drawer.
  const resetRun = useCallback(() => {
    runToken.current += 1;
    setRun(null);
    setRunId(null);
    setRunLoading(false);
  }, []);

  const openTask = useCallback(
    async (taskId: string, initialRunId?: string) => {
      const token = ++detailToken.current;
      setDetailId(taskId);
      setDetailLoading(true);
      setDetail(null);
      resetRun();
      try {
        const next = await api<EvalTaskDetail>(
          `/api/evaluation/tasks/${encodeURIComponent(taskId)}`,
        );
        if (token !== detailToken.current) return;
        setDetail(next);
        if (initialRunId) void openRun(initialRunId);
      } catch {
        if (token === detailToken.current) setDetail(null);
      } finally {
        if (token === detailToken.current) setDetailLoading(false);
      }
    },
    [openRun, resetRun],
  );

  const closeTask = useCallback(() => {
    detailToken.current += 1;
    setDetailId(null);
    setDetail(null);
    setDetailLoading(false);
    resetRun();
  }, [resetRun]);

  return {
    filters,
    setFilters,
    taskQuery,
    setTaskQuery,
    overview,
    board,
    tasks,
    benchmarks,
    diagnoses,
    loading,
    tasksLoading,
    error,
    refresh,
    detail,
    detailId,
    detailLoading,
    openTask,
    closeTask,
    run,
    runId,
    runLoading,
    openRun,
    closeRun,
  };
}
