import type { MessageKey } from "@/lib/i18n";

/**
 * The console's destination table. Keep this module free of component
 * imports: `app/(console)/[...slug]/page.tsx` calls `generateStaticParams`
 * from it while running on the server.
 */

export type RouteId =
  | "chat"
  | "board"
  | "files"
  | "observability"
  | "schedules"
  | "settings";

export type NavIconId = RouteId;

/** The sidebar body that belongs to a destination, if any. */
export type PanelId = "sessions" | "board" | null;

/** The title-bar shortcut a destination offers. */
export type RoutePrimary = "newChat" | "newTask";

export interface RouteDef {
  id: RouteId;
  path: string;
  panel: PanelId;
  nav?: { labelKey: MessageKey; icon: NavIconId };
  titleKey?: MessageKey;
  primary?: RoutePrimary;
}

export const ROUTES: RouteDef[] = [
  {
    id: "chat",
    path: "/",
    panel: "sessions",
    nav: { labelKey: "navChat", icon: "chat" },
    primary: "newChat",
  },
  {
    id: "board",
    path: "/board",
    panel: "board",
    titleKey: "boardTitle",
    nav: { labelKey: "navBoard", icon: "board" },
    primary: "newTask",
  },
  {
    id: "files",
    path: "/files",
    panel: null,
    titleKey: "navFiles",
    nav: { labelKey: "navFiles", icon: "files" },
  },
  {
    id: "observability",
    path: "/observability",
    panel: null,
    titleKey: "navObservability",
    nav: { labelKey: "navObservability", icon: "observability" },
  },
  {
    id: "schedules",
    path: "/schedules",
    panel: null,
    titleKey: "navSchedules",
    nav: { labelKey: "navSchedules", icon: "schedules" },
  },
  {
    id: "settings",
    path: "/settings",
    panel: null,
    titleKey: "settings",
    nav: { labelKey: "settings", icon: "settings" },
  },
];

/** `/board/` and `/board` are the same destination. */
function normalizePath(path: string): string {
  return path.length > 1 ? path.replace(/\/+$/, "") : path;
}

export function matchRoute(path: string): RouteDef | null {
  const target = normalizePath(path);
  return ROUTES.find((route) => route.path === target) ?? null;
}

export function routeById(id: RouteId): RouteDef {
  const route = ROUTES.find((item) => item.id === id);
  if (!route) throw new Error(`Unknown route: ${id}`);
  return route;
}

/**
 * Chat links carry the selected conversation, so returning from another
 * page keeps the same conversation open.
 */
export function navHref(route: RouteDef, sessionId: string | null): string {
  if (route.id === "chat" && sessionId) {
    return `${route.path}?session=${encodeURIComponent(sessionId)}`;
  }
  return route.path;
}
