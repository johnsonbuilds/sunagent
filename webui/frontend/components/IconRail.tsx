"use client";

import type { ReactElement } from "react";

import { useI18n, type MessageKey } from "@/lib/i18n";

import { ChatIcon, SettingsIcon } from "./Icons";

export type PanelId = "sessions" | "settings";

interface NavItem {
  id: PanelId;
  labelKey: MessageKey;
  Icon: () => ReactElement;
}

/** Add new sidebar destinations here; the rail and panel switch follow. */
export const NAV_ITEMS: NavItem[] = [
  { id: "sessions", labelKey: "conversations", Icon: ChatIcon },
  { id: "settings", labelKey: "settings", Icon: SettingsIcon },
];

interface IconRailProps {
  active: PanelId;
  onSelect: (id: PanelId) => void;
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
