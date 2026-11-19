import type { ComponentType } from "react";

import type { RouteId } from "@/lib/routes";
import BoardPage from "@/views/BoardPage";
import ChatView from "@/views/ChatView";
import FilesPage from "@/views/FilesPage";
import ObservabilityPage from "@/views/ObservabilityPage";
import SchedulesPage from "@/views/SchedulesPage";
import SettingsPage from "@/views/SettingsPage";

export const VIEWS: Record<RouteId, ComponentType> = {
  chat: ChatView,
  board: BoardPage,
  files: FilesPage,
  observability: ObservabilityPage,
  schedules: SchedulesPage,
  settings: SettingsPage,
};
