/** Shared board vocabulary: order, labels, and display weights. */

import type { MessageKey, TFunction } from "@/lib/i18n";
import type { BoardTask, TaskPriority, TaskStatus } from "@/lib/types";

/** Human-readable age, matching the conversation list's wording. */
export function relativeTime(epochSeconds: number, t: TFunction): string {
  const delta = Date.now() / 1000 - epochSeconds;
  if (delta < 60) return t("timeNow");
  if (delta < 3600) {
    return t("timeMinutes", { count: Math.floor(delta / 60) });
  }
  if (delta < 86400) {
    return t("timeHours", { count: Math.floor(delta / 3600) });
  }
  return t("timeDays", { count: Math.floor(delta / 86400) });
}

export const STATUS_ORDER: TaskStatus[] = [
  "backlog",
  "todo",
  "doing",
  "review",
  "done",
];

export const STATUS_LABEL_KEYS: Record<TaskStatus, MessageKey> = {
  backlog: "boardColumnBacklog",
  todo: "boardColumnTodo",
  doing: "boardColumnDoing",
  review: "boardColumnReview",
  done: "boardColumnDone",
};

export const PRIORITY_ORDER: TaskPriority[] = [
  "low",
  "normal",
  "high",
  "urgent",
];

export const PRIORITY_LABEL_KEYS: Record<TaskPriority, MessageKey> = {
  low: "boardPriorityLow",
  normal: "boardPriorityNormal",
  high: "boardPriorityHigh",
  urgent: "boardPriorityUrgent",
};

export const PRIORITY_WEIGHT: Record<TaskPriority, number> = {
  low: 0,
  normal: 1,
  high: 2,
  urgent: 3,
};

/** Execution order of explicitly queued Todo cards. */
export function queueOrder(tasks: BoardTask[]): Map<string, number> {
  const queued = tasks
    .filter((task) => task.status === "todo" && task.queued_at != null)
    .slice()
    .sort((a, b) => (
      PRIORITY_WEIGHT[b.priority] - PRIORITY_WEIGHT[a.priority]
      || a.position - b.position
      || a.created_at - b.created_at
    ));
  return new Map(queued.map((task, index) => [task.id, index + 1]));
}