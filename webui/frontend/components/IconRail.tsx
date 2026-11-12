"use client";

import type { ReactElement } from "react";

import { ChatIcon, SettingsIcon } from "./Icons";

export type PanelId = "sessions" | "settings";

interface NavItem {
  id: PanelId;
  label: string;
  Icon: () => ReactElement;
}

/** Add new sidebar destinations here; the rail and panel switch follow. */
export const NAV_ITEMS: NavItem[] = [
  { id: "sessions", label: "会话", Icon: ChatIcon },
  { id: "settings", label: "设置", Icon: SettingsIcon },
];

interface IconRailProps {
  active: PanelId;
  onSelect: (id: PanelId) => void;
}

export default function IconRail({ active, onSelect }: IconRailProps) {
  return (
    <nav className="rail" aria-label="Sidebar navigation">
      <div className="rail-group">
        {NAV_ITEMS.map(({ id, label, Icon }) => {
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
