import type { ComponentType } from "react";

import type { RouteId } from "@/lib/routes";
import BoardPage from "@/views/BoardPage";
import ChatView from "@/views/ChatView";
import FilesPage from "@/views/FilesPage";
import EvaluationPage from "@/views/EvaluationPage";
import SchedulesPage from "@/views/SchedulesPage";
import SettingsPage from "@/views/SettingsPage";

export const VIEWS: Record<RouteId, ComponentType> = {
  chat: ChatView,
  board: BoardPage,
  files: FilesPage,
  evaluation: EvaluationPage,
  schedules: SchedulesPage,
  settings: SettingsPage,
};
