"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useState,
  type ReactNode,
} from "react";

import { useBoard } from "@/lib/useBoard";
import type { BoardTask, TaskInput, TaskStatus } from "@/lib/types";

import { useSessions } from "./sessions";
import { useToast } from "./toast";

type EditorState =
  | { mode: "create"; status: TaskStatus }
  | { mode: "edit"; taskId: string; status: TaskStatus }
  | null;

type BoardData = ReturnType<typeof useBoard>;

interface BoardActions {
  editorOpen: boolean;
  editingTask: BoardTask | null;
  editorInitialStatus: TaskStatus;
  detailTask: BoardTask | null;
  deleteTarget: BoardTask | null;
  deleting: boolean;
  openCreateTask: (status: TaskStatus) => void;
  openEditTask: (task: BoardTask) => void;
  openTaskDetail: (task: BoardTask) => void;
  closeEditor: () => void;
  closeDetail: () => void;
  requestDeleteTask: (task: BoardTask) => void;
  confirmDeleteTask: () => Promise<void>;
  cancelDeleteTask: () => void;
  saveTask: (input: TaskInput) => Promise<void>;
  openTaskConversation: (task: BoardTask) => Promise<string | null>;
}

type BoardContextValue = BoardData & BoardActions;

const BoardContext = createContext<BoardContextValue | null>(null);

export function BoardProvider({
  fastPoll,
  children,
}: {
  fastPoll: boolean;
  children: ReactNode;
}) {
  const { showToast } = useToast();
  const board = useBoard({ onError: showToast, fastPoll });
  const { boardSyncSeq, refreshSessions } = useSessions();

  const [editor, setEditor] = useState<EditorState>(null);
  const [detailTaskId, setDetailTaskId] = useState<string | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<BoardTask | null>(null);
  const [deleting, setDeleting] = useState(false);

  useEffect(() => {
    if (boardSyncSeq === 0) return;
    void board.refresh(true);
  }, [boardSyncSeq, board.refresh]);

  const openCreateTask = useCallback((status: TaskStatus) => {
    setEditor({ mode: "create", status });
  }, []);

  const openEditTask = useCallback((task: BoardTask) => {
    setDetailTaskId(null);
    setEditor({ mode: "edit", taskId: task.id, status: task.status });
  }, []);

  const openTaskDetail = useCallback((task: BoardTask) => {
    setEditor(null);
    setDetailTaskId(task.id);
  }, []);

  const closeEditor = useCallback(() => setEditor(null), []);
  const closeDetail = useCallback(() => setDetailTaskId(null), []);

  const requestDeleteTask = useCallback((task: BoardTask) => {
    setEditor(null);
    setDetailTaskId(null);
    setDeleteTarget(task);
  }, []);

  const cancelDeleteTask = useCallback(() => {
    if (!deleting) setDeleteTarget(null);
  }, [deleting]);

  const confirmDeleteTask = useCallback(async () => {
    if (!deleteTarget) return;
    setDeleting(true);
    try {
      await board.deleteTask(deleteTarget.id);
      setDeleteTarget(null);
    } catch {
    } finally {
      setDeleting(false);
    }
  }, [board, deleteTarget]);

  const saveTask = useCallback(
    async (input: TaskInput) => {
      if (!editor) return;
      if (editor.mode === "create") {
        await board.createTask(input);
        setEditor(null);
        return;
      }

      const existing = board.snapshot?.tasks.find(
        (task) => task.id === editor.taskId,
      );
      const patch: Partial<TaskInput> = {};
      if (!existing || input.title !== existing.title) {
        patch.title = input.title;
      }
      if (!existing || input.description !== existing.description) {
        patch.description = input.description;
      }
      if (
        input.status !== editor.status
        && (!existing || input.status !== existing.status)
      ) {
        patch.status = input.status;
      }
      if (!existing || input.priority !== existing.priority) {
        patch.priority = input.priority;
      }
      if ((existing?.model ?? "") !== input.model) patch.model = input.model;
      if ((existing?.harness ?? "") !== input.harness) {
        patch.harness = input.harness;
      }
      if (
        !existing
        || JSON.stringify(input.tags) !== JSON.stringify(existing.tags)
      ) {
        patch.tags = input.tags;
      }
      if (Object.keys(patch).length === 0) {
        setEditor(null);
        return;
      }
      await board.updateTask(editor.taskId, patch);
      setEditor(null);
    },
    [board, editor],
  );

  const openTaskConversation = useCallback(
    async (task: BoardTask): Promise<string | null> => {
      try {
        let sessionId = task.session_id;
        if (!sessionId || !task.session) {
          const result = await board.ensureSession(task.id);
          sessionId = result.session_id;
        }
        if (!sessionId) return null;
        setEditor(null);
        setDetailTaskId(null);
        await refreshSessions();
        return sessionId;
      } catch {
        return null;
      }
    },
    [board, refreshSessions],
  );

  useEffect(() => {
    if (!board.snapshot) return;
    const ids = new Set(board.snapshot.tasks.map((task) => task.id));
    if (editor?.mode === "edit" && !ids.has(editor.taskId)) {
      setEditor(null);
    }
    if (detailTaskId && !ids.has(detailTaskId)) {
      setDetailTaskId(null);
    }
    if (deleteTarget && !ids.has(deleteTarget.id)) {
      setDeleteTarget(null);
    }
  }, [board.snapshot, editor, detailTaskId, deleteTarget]);

  const editingTask =
    editor?.mode === "edit"
      ? board.snapshot?.tasks.find((task) => task.id === editor.taskId) ?? null
      : null;
  const editorOpen =
    editor !== null
    && (editor.mode === "create" || editingTask !== null);
  const editorInitialStatus =
    editor?.mode === "create"
      ? editor.status
      : editingTask?.status ?? "backlog";
  const detailTask = detailTaskId
    ? board.snapshot?.tasks.find((task) => task.id === detailTaskId) ?? null
    : null;

  const value = useMemo<BoardContextValue>(
    () => ({
      ...board,
      editorOpen,
      editingTask,
      editorInitialStatus,
      detailTask,
      deleteTarget,
      deleting,
      openCreateTask,
      openEditTask,
      openTaskDetail,
      closeEditor,
      closeDetail,
      requestDeleteTask,
      confirmDeleteTask,
      cancelDeleteTask,
      saveTask,
      openTaskConversation,
    }),
    [
      board,
      editorOpen,
      editingTask,
      editorInitialStatus,
      detailTask,
      deleteTarget,
      deleting,
      openCreateTask,
      openEditTask,
      openTaskDetail,
      closeEditor,
      closeDetail,
      requestDeleteTask,
      confirmDeleteTask,
      cancelDeleteTask,
      saveTask,
      openTaskConversation,
    ],
  );

  return (
    <BoardContext.Provider value={value}>{children}</BoardContext.Provider>
  );
}

export function useBoardStore(): BoardContextValue {
  const value = useContext(BoardContext);
  if (!value) throw new Error("useBoardStore must be used inside BoardProvider");
  return value;
}
