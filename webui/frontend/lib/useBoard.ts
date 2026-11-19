"use client";

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";

import { api } from "@/lib/api";
import { tasksEnteredReview } from "@/lib/board";
import {
  playNotificationChime,
  primeNotificationSound,
} from "@/lib/sound";
import type {
  BoardSnapshot,
  CommentResult,
  DeleteTaskResult,
  EnsureSessionResult,
  TaskDetail,
  TaskInput,
  TaskStatus,
} from "@/lib/types";

const POLL_FAST_MS = 5000;
const POLL_SLOW_MS = 25000;

interface UseBoardOptions {
  onError?: (message: string) => void;
  fastPoll?: boolean;
}

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function moveOptimistic(
  snapshot: BoardSnapshot,
  taskId: string,
  status: TaskStatus,
  position: number,
): BoardSnapshot {
  const task = snapshot.tasks.find((item) => item.id === taskId);
  if (!task) return snapshot;

  const remaining = snapshot.tasks
    .filter((item) => item.id !== taskId)
    .map((item) => ({ ...item }));
  const column = remaining
    .filter((item) => item.status === status)
    .sort((a, b) => a.position - b.position);
  const index = Math.max(0, Math.min(position, column.length));
  column.splice(index, 0, { ...task, status });

  const others = remaining.filter((item) => item.status !== status);
  const tasks = [...others, ...column].sort(
    (a, b) => snapshot.statuses.indexOf(a.status) - snapshot.statuses.indexOf(b.status),
  );

  const counters = new Map<TaskStatus, number>(
    snapshot.statuses.map((item) => [item, 0]),
  );
  for (const item of tasks) {
    const next = counters.get(item.status) ?? 0;
    item.position = next;
    counters.set(item.status, next + 1);
  }

  return { ...snapshot, tasks };
}

export function useBoard({ onError, fastPoll = false }: UseBoardOptions) {
  const [snapshot, setSnapshot] = useState<BoardSnapshot | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState("");
  const [hideDone, setHideDone] = useState(false);

  const snapshotRef = useRef<BoardSnapshot | null>(null);
  const pendingRef = useRef(0);
  const epochRef = useRef(0);
  const loadedRef = useRef(false);
  const onErrorRef = useRef(onError);
  onErrorRef.current = onError;

  const report = useCallback((failure: unknown) => {
    const message = errorMessage(failure);
    setError(message);
    onErrorRef.current?.(message);
  }, []);

  const apply = useCallback((next: BoardSnapshot) => {
    snapshotRef.current = next;
    setSnapshot(next);
    setError(null);
    loadedRef.current = true;
  }, []);

  const notifyReview = useCallback(
    (before: BoardSnapshot | null, after: BoardSnapshot) => {
      if (tasksEnteredReview(before, after).length > 0) {
        playNotificationChime();
      }
    },
    [],
  );

  const refresh = useCallback(async (force = false) => {
    if (!force && pendingRef.current > 0) return;
    if (!loadedRef.current) setLoading(true);
    const epoch = epochRef.current;
    try {
      const next = await api<BoardSnapshot>("/api/board");
      if (pendingRef.current > 0 || epochRef.current !== epoch) return;
      const current = snapshotRef.current;
      if (!current || JSON.stringify(current) !== JSON.stringify(next)) {
        snapshotRef.current = next;
        setSnapshot(next);
        notifyReview(current, next);
      }
      setError(null);
      loadedRef.current = true;
    } catch (failure) {
      setError(errorMessage(failure));
    } finally {
      setLoading(false);
    }
  }, [notifyReview]);

  const mutate = useCallback(async <T,>(path: string,
                                        init: RequestInit): Promise<T> => {
    epochRef.current += 1;
    pendingRef.current += 1;
    try {
      return await api<T>(path, init);
    } finally {
      pendingRef.current = Math.max(0, pendingRef.current - 1);
    }
  }, []);

  const createTask = useCallback(async (input: TaskInput) => {
    try {
      const next = await mutate<BoardSnapshot>("/api/board/tasks", {
        method: "POST",
        body: JSON.stringify(input),
      });
      apply(next);
    } catch (failure) {
      report(failure);
      throw failure;
    }
  }, [apply, mutate, report]);

  const updateTask = useCallback(
    async (taskId: string, patch: Partial<TaskInput>) => {
      const previous = snapshotRef.current;
      try {
        const next = await mutate<BoardSnapshot>(`/api/board/tasks/${taskId}`, {
          method: "PATCH",
          body: JSON.stringify(patch),
        });
        apply(next);
        notifyReview(previous, next);
      } catch (failure) {
        report(failure);
        throw failure;
      }
    },
    [apply, mutate, notifyReview, report],
  );

  const deleteTask = useCallback(async (taskId: string) => {
    try {
      const result = await mutate<DeleteTaskResult>(
        `/api/board/tasks/${taskId}`,
        { method: "DELETE" },
      );
      apply(result.board);
      return result.deleted_session;
    } catch (failure) {
      report(failure);
      throw failure;
    }
  }, [apply, mutate, report]);

  const moveTask = useCallback(
    async (taskId: string, status: TaskStatus, position: number) => {
      const previous = snapshotRef.current;
      if (previous) {
        const optimistic = moveOptimistic(previous, taskId, status, position);
        snapshotRef.current = optimistic;
        setSnapshot(optimistic);
      }
      try {
        const next = await mutate<BoardSnapshot>(`/api/board/tasks/${taskId}`, {
          method: "PATCH",
          body: JSON.stringify({ status, position }),
        });
        apply(next);
        notifyReview(previous, next);
      } catch (failure) {
        if (previous) {
          snapshotRef.current = previous;
          setSnapshot(previous);
        }
        report(failure);
        void refresh(true);
        throw failure;
      }
    },
    [apply, mutate, notifyReview, refresh, report],
  );

  const ensureSession = useCallback(async (taskId: string) => {
    try {
      const result = await mutate<EnsureSessionResult>(
        `/api/board/tasks/${taskId}/session`,
        { method: "POST", body: "{}" },
      );
      apply(result.board);
      return result;
    } catch (failure) {
      report(failure);
      throw failure;
    }
  }, [apply, mutate, report]);

  const setPaused = useCallback(async (paused: boolean) => {
    try {
      const next = await mutate<BoardSnapshot>("/api/board/executor", {
        method: "PATCH",
        body: JSON.stringify({ paused }),
      });
      apply(next);
    } catch (failure) {
      report(failure);
      throw failure;
    }
  }, [apply, mutate, report]);

  const taskDetail = useCallback(
    async (taskId: string) =>
      api<TaskDetail>(`/api/board/tasks/${taskId}`),
    [],
  );

  const addComment = useCallback(
    async (taskId: string, body: string) => {
      try {
        const result = await mutate<CommentResult>(
          `/api/board/tasks/${taskId}/comments`,
          { method: "POST", body: JSON.stringify({ body }) },
        );
        apply(result.board);
        return result;
      } catch (failure) {
        report(failure);
        throw failure;
      }
    },
    [apply, mutate, report],
  );

  useEffect(() => {
    primeNotificationSound();
    const interval = fastPoll ? POLL_FAST_MS : POLL_SLOW_MS;
    void refresh();
    const timer = window.setInterval(() => {
      if (document.visibilityState === "visible") void refresh();
    }, interval);
    const onVisible = () => {
      if (document.visibilityState === "visible") void refresh();
    };
    window.addEventListener("focus", onVisible);
    document.addEventListener("visibilitychange", onVisible);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener("focus", onVisible);
      document.removeEventListener("visibilitychange", onVisible);
    };
  }, [fastPoll, refresh]);

  return useMemo(
    () => ({
      snapshot,
      loading,
      error,
      filter,
      setFilter,
      hideDone,
      setHideDone,
      refresh,
      createTask,
      updateTask,
      deleteTask,
      moveTask,
      ensureSession,
      setPaused,
      taskDetail,
      addComment,
    }),
    [
      snapshot,
      loading,
      error,
      filter,
      hideDone,
      refresh,
      createTask,
      updateTask,
      deleteTask,
      moveTask,
      ensureSession,
      setPaused,
      taskDetail,
      addComment,
    ],
  );
}