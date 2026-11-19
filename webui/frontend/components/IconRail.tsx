"use client";

import Link from "next/link";
import { usePathname } from "next/navigation";
import type { ReactElement } from "react";

import { useI18n } from "@/lib/i18n";
import { matchRoute, navHref, ROUTES, type NavIconId } from "@/lib/routes";
import { useSessions } from "@/lib/stores/sessions";

import {
  BoardIcon,
  ChatIcon,
  FilesIcon,
  ObservabilityIcon,
  SchedulesIcon,
  SettingsIcon,
} from "./Icons";

const NAV_ICONS: Record<NavIconId, () => ReactElement> = {
  chat: ChatIcon,
  board: BoardIcon,
  files: FilesIcon,
  observability: ObservabilityIcon,
  schedules: SchedulesIcon,
  settings: SettingsIcon,
};

const NAV_ROUTES = ROUTES.filter((route) => route.nav);

interface IconRailProps {
  onNavigate: () => void;
}

export default function IconRail({ onNavigate }: IconRailProps) {
  const { t } = useI18n();
  const pathname = usePathname();
  const { activeId } = useSessions();
  const activeRoute = matchRoute(pathname);

  return (
    <nav className="rail" aria-label={t("navSidebarLabel")}>
      <div className="rail-group">
        {NAV_ROUTES.map((route) => {
          if (!route.nav) return null;
          const label = t(route.nav.labelKey);
          const isActive = route.id === activeRoute?.id;
          const Icon = NAV_ICONS[route.nav.icon];
          return (
            <Link
              key={route.id}
              className={`rail-btn${isActive ? " active" : ""}`}
              href={navHref(route, activeId)}
              title={label}
              aria-label={label}
              aria-current={isActive ? "page" : undefined}
              onClick={onNavigate}
            >
              <Icon />
            </Link>
          );
        })}
      </div>
    </nav>
  );
}
