"use client";

import type { SessionSummary } from "@/lib/types";

import IconRail, { type PanelId } from "./IconRail";
import SessionsPanel from "./SessionsPanel";
import SettingsPanel from "./SettingsPanel";

interface SidebarProps {
  sessions: SessionSummary[];
  activeId: string | null;
  filter: string;
  activePanel: PanelId;
  collapsed: boolean;
  backdropVisible: boolean;
  onSelectPanel: (id: PanelId) => void;
  onFilterChange: (value: string) => void;
  onSelect: (id: string) => void;
  onNewChat: () => void;
  onDelete: (session: SessionSummary) => void;
  onRename: (session: SessionSummary) => void;
  onRestart: () => void;
  onBackdropClick: () => void;
}

export default function Sidebar({
  sessions,
  activeId,
  filter,
  activePanel,
  collapsed,
  backdropVisible,
  onSelectPanel,
  onFilterChange,
  onSelect,
  onNewChat,
  onDelete,
  onRename,
  onRestart,
  onBackdropClick,
}: SidebarProps) {
  return (
    <>
      <div
        className="sidebar-backdrop"
        id="sidebarBackdrop"
        hidden={!backdropVisible}
        onClick={onBackdropClick}
      />
      <aside className={`sidebar${collapsed ? " collapsed" : ""}`} id="sidebar">
        <IconRail active={activePanel} onSelect={onSelectPanel} />
        <div className="sidebar-content">
          {activePanel === "sessions" ? (
            <SessionsPanel
              sessions={sessions}
              activeId={activeId}
              filter={filter}
              onFilterChange={onFilterChange}
              onSelect={onSelect}
              onNewChat={onNewChat}
              onDelete={onDelete}
              onRename={onRename}
            />
          ) : (
            <SettingsPanel onRestart={onRestart} />
          )}
        </div>
      </aside>
    </>
  );
}
