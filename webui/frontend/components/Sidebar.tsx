"use client";

import type { BoardSnapshot, SessionSummary } from "@/lib/types";

import BoardPanel from "./BoardPanel";
import IconRail, { type AppView, type NavId, type PanelId } from "./IconRail";
import SessionsPanel from "./SessionsPanel";
import SettingsPanel from "./SettingsPanel";

interface SidebarProps {
  view: AppView;
  sessions: SessionSummary[];
  activeId: string | null;
  filter: string;
  activePanel: PanelId;
  board: BoardSnapshot | null;
  boardLoading: boolean;
  boardFilter: string;
  hideDone: boolean;
  collapsed: boolean;
  backdropVisible: boolean;
  onSelectNav: (id: NavId) => void;
  onFilterChange: (value: string) => void;
  onBoardFilterChange: (value: string) => void;
  onHideDoneChange: (value: boolean) => void;
  onSelect: (id: string) => void;
  onNewChat: () => void;
  onNewTask: () => void;
  onDelete: (session: SessionSummary) => void;
  onRename: (session: SessionSummary) => void;
  onRestart: () => void;
  onBackdropClick: () => void;
}

export default function Sidebar({
  view,
  sessions,
  activeId,
  filter,
  activePanel,
  board,
  boardLoading,
  boardFilter,
  hideDone,
  collapsed,
  backdropVisible,
  onSelectNav,
  onFilterChange,
  onBoardFilterChange,
  onHideDoneChange,
  onSelect,
  onNewChat,
  onNewTask,
  onDelete,
  onRename,
  onRestart,
  onBackdropClick,
}: SidebarProps) {
  const activeNav: NavId =
    activePanel === "settings"
      ? "settings"
      : view === "board"
        ? "board"
        : "chat";

  return (
    <>
      <div
        className="sidebar-backdrop"
        id="sidebarBackdrop"
        hidden={!backdropVisible}
        onClick={onBackdropClick}
      />
      <aside className={`sidebar${collapsed ? " collapsed" : ""}`} id="sidebar">
        <IconRail active={activeNav} onSelect={onSelectNav} />
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
          ) : activePanel === "board" ? (
            <BoardPanel
              snapshot={board}
              loading={boardLoading}
              filter={boardFilter}
              hideDone={hideDone}
              onFilterChange={onBoardFilterChange}
              onHideDoneChange={onHideDoneChange}
              onNewTask={onNewTask}
            />
          ) : (
            <SettingsPanel onRestart={onRestart} />
          )}
        </div>
      </aside>
    </>
  );
}