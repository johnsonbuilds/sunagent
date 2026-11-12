"use client";

import type { SessionSummary } from "@/lib/types";
import { CloseIcon, PlusIcon, SearchIcon, SettingsIcon } from "./Icons";

interface SidebarProps {
  sessions: SessionSummary[];
  activeId: string | null;
  filter: string;
  collapsed: boolean;
  backdropVisible: boolean;
  onFilterChange: (value: string) => void;
  onSelect: (id: string) => void;
  onNewChat: () => void;
  onDelete: (session: SessionSummary) => void;
  onRename: (session: SessionSummary) => void;
  onOpenSettings: () => void;
  onBackdropClick: () => void;
}

function relativeTime(epochSeconds: number): string {
  const delta = Date.now() / 1000 - epochSeconds;
  if (delta < 60) return "now";
  if (delta < 3600) return `${Math.floor(delta / 60)}m`;
  if (delta < 86400) return `${Math.floor(delta / 3600)}h`;
  return `${Math.floor(delta / 86400)}d`;
}

export default function Sidebar({
  sessions,
  activeId,
  filter,
  collapsed,
  backdropVisible,
  onFilterChange,
  onSelect,
  onNewChat,
  onDelete,
  onRename,
  onOpenSettings,
  onBackdropClick,
}: SidebarProps) {
  const needle = filter.trim().toLowerCase();
  const visible = sessions.filter(
    (session) =>
      !needle || (session.title || "").toLowerCase().indexOf(needle) !== -1,
  );

  return (
    <>
      <div
        className="sidebar-backdrop"
        id="sidebarBackdrop"
        hidden={!backdropVisible}
        onClick={onBackdropClick}
      />
      <aside className={`sidebar${collapsed ? " collapsed" : ""}`} id="sidebar">
        <div className="sidebar-head">
          <span className="sidebar-label">Conversations</span>
          <button
            className="icon-btn small"
            id="btnNewChatSide"
            title="New conversation"
            aria-label="New conversation"
            onClick={onNewChat}
          >
            <PlusIcon />
          </button>
        </div>
        <div className="sidebar-search">
          <SearchIcon />
          <input
            id="sessionSearch"
            type="search"
            placeholder="Filter conversations…"
            autoComplete="off"
            spellCheck={false}
            value={filter}
            onChange={(event) => onFilterChange(event.target.value)}
          />
        </div>
        <div className="session-list" id="sessionList">
          {visible.map((session) => (
            <div
              key={session.id}
              className={`session-item${
                session.id === activeId ? " active" : ""
              }`}
              title={session.title}
              onClick={() => onSelect(session.id)}
            >
              <span
                className="session-title"
                onDoubleClick={(event) => {
                  event.stopPropagation();
                  onRename(session);
                }}
              >
                {session.title || "Untitled"}
              </span>
              <span className="session-time">
                {relativeTime(session.updated_at)}
              </span>
              <button
                className="session-delete"
                type="button"
                title="Delete conversation"
                onClick={(event) => {
                  event.stopPropagation();
                  onDelete(session);
                }}
              >
                <CloseIcon size={13} />
              </button>
            </div>
          ))}
        </div>
        <div className="sidebar-foot">
          <button
            className="settings-trigger"
            id="btnSettings"
            type="button"
            title="Settings"
            aria-label="Settings"
            onClick={onOpenSettings}
          >
            <SettingsIcon />
          </button>
        </div>
      </aside>
    </>
  );
}
