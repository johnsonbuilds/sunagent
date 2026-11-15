import type { ComponentType } from "react";

import type { RouteId } from "@/lib/routes";
import BoardPage from "@/views/BoardPage";
import ChatView from "@/views/ChatView";
import FilesPage from "@/views/FilesPage";
import ObservabilityPage from "@/views/ObservabilityPage";
import SchedulesPage from "@/views/SchedulesPage";
import SettingsPage from "@/views/SettingsPage";

/**
 * The only place a route id is turned into a view. A missing entry is a
 * compile error, so the rail can never link to a route without a page.
 */
export const VIEWS: Record<RouteId, ComponentType> = {
  chat: ChatView,
  board: BoardPage,
  files: FilesPage,
  observability: ObservabilityPage,
  schedules: SchedulesPage,
  settings: SettingsPage,
};
