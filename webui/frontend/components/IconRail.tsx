"use client";

import type { ReactElement } from "react";

import { useI18n, type MessageKey } from "@/lib/i18n";

import { BoardIcon, ChatIcon, SettingsIcon } from "./Icons";

export type AppView = "chat" | "board";

/** Optional sidebar content. Settings can sit over either app view. */
export type PanelId = "sessions" | "board" | "settings";

/** Top-level destinations in the rail. */
export type NavId = "chat" | "board" | "settings";

interface NavItem {
  id: NavId;
  labelKey: MessageKey;
  Icon: () => ReactElement;
}

/** Add new sidebar destinations here; the rail and panel switch follow. */
export const NAV_ITEMS: NavItem[] = [
  { id: "chat", labelKey: "navChat", Icon: ChatIcon },
  { id: "board", labelKey: "navBoard", Icon: BoardIcon },
  { id: "settings", labelKey: "settings", Icon: SettingsIcon },
];

interface IconRailProps {
  active: NavId;
  onSelect: (id: NavId) => void;
}

export default function IconRail({ active, onSelect }: IconRailProps) {
  const { t } = useI18n();

  return (
    <nav className="rail" aria-label={t("navSidebarLabel")}>
      <div className="rail-group">
        {NAV_ITEMS.map(({ id, labelKey, Icon }) => {
          const label = t(labelKey);
          const isActive = id === active;
          return (
            <button
              key={id}
              type="button"
              className={`rail-btn${isActive ? " active" : ""}`}
              title={label}
              aria-label={label}
              aria-current={isActive ? "page" : undefined}
              onClick={() => onSelect(id)}
            >
              <Icon />
            </button>
          );
        })}
      </div>
    </nav>
  );
}
