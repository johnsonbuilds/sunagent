"use client";

import { useRouter } from "next/navigation";

import BoardView from "@/components/BoardView";
import ConfirmDialog from "@/components/ConfirmDialog";
import { useShell } from "@/components/shell/AppShell";
import TaskDetail from "@/components/TaskDetail";
import TaskEditor from "@/components/TaskEditor";
import { STATUS_ORDER } from "@/lib/board";
import { useI18n } from "@/lib/i18n";
import { useBoardStore } from "@/lib/stores/board";
import { useRuntimeConfig } from "@/lib/stores/runtime-config";
import type { BoardTask } from "@/lib/types";

export default function BoardPage() {
  const { t } = useI18n();
  const shell = useShell();
  const router = useRouter();
  const { models, harnesses } = useRuntimeConfig();
  const board = useBoardStore();

  const paused = board.snapshot?.executor?.paused ?? false;

  const openConversation = (task: BoardTask) => {
    void board.openTaskConversation(task).then((sessionId) => {
      if (!sessionId) return;
      router.push(`/?session=${encodeURIComponent(sessionId)}`);
      if (shell.narrow) shell.closeSidebar();
    });
  };

  return (
    <>
      <BoardView
        snapshot={board.snapshot}
        loading={board.loading}
        error={board.error}
        filter={board.filter}
        hideDone={board.hideDone}
        paused={paused}
        onTogglePaused={() => void board.setPaused(!paused)}
        onRefresh={() => void board.refresh(true)}
        onCreateTask={board.openCreateTask}
        onEditTask={board.openEditTask}
        onViewTask={board.openTaskDetail}
        onDeleteTask={board.requestDeleteTask}
        onOpenTask={openConversation}
        onMoveTask={board.moveTask}
      />

      <TaskEditor
        open={board.editorOpen}
        task={board.editingTask}
        initialStatus={board.editorInitialStatus}
        statuses={board.snapshot?.statuses ?? STATUS_ORDER}
        models={models}
        harnesses={harnesses}
        onClose={board.closeEditor}
        onSave={board.saveTask}
        onRequestDelete={board.requestDeleteTask}
        onOpenConversation={openConversation}
      />

      <TaskDetail
        open={board.detailTask !== null}
        task={board.detailTask}
        onClose={board.closeDetail}
        onEdit={board.openEditTask}
        onOpenConversation={openConversation}
        onFetch={board.taskDetail}
        onComment={board.addComment}
      />

      <ConfirmDialog
        open={board.deleteTarget !== null}
        title={t("boardDeleteTask")}
        message={t("boardDeleteTaskConfirm", {
          title: board.deleteTarget?.title ?? "",
        })}
        confirmText={t("boardDeleteTask")}
        cancelText={t("defaultCancel")}
        busyText={t("defaultBusy")}
        tone="danger"
        busy={board.deleting}
        onConfirm={() => void board.confirmDeleteTask()}
        onCancel={board.cancelDeleteTask}
      />
    </>
  );
}
