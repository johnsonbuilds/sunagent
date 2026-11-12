"use client";

import type { SessionSummary } from "@/lib/types";
import { useI18n, type TFunction } from "@/lib/i18n";
import { CloseIcon, PlusIcon, SearchIcon } from "./Icons";

interface SessionsPanelProps {
  sessions: SessionSummary[];
  activeId: string | null;
  filter: string;
  onFilterChange: (value: string) => void;
  onSelect: (id: string) => void;
  onNewChat: () => void;
  onDelete: (session: SessionSummary) => void;
  onRename: (session: SessionSummary) => void;
}

function relativeTime(epochSeconds: number, t: TFunction): string {
  const delta = Date.now() / 1000 - epochSeconds;
  if (delta < 60) return t("timeNow");
  if (delta < 3600) {
    return t("timeMinutes", { count: Math.floor(delta / 60) });
  }
  if (delta < 86400) {
    return t("timeHours", { count: Math.floor(delta / 3600) });
  }
  return t("timeDays", { count: Math.floor(delta / 86400) });
}

export default function SessionsPanel({
  sessions,
  activeId,
  filter,
  onFilterChange,
  onSelect,
  onNewChat,
  onDelete,
  onRename,
}: SessionsPanelProps) {
  const { t } = useI18n();
  const needle = filter.trim().toLowerCase();
  const visible = sessions.filter(
    (session) =>
      !needle || (session.title || "").toLowerCase().indexOf(needle) !== -1,
  );

  return (
    <div className="panel">
      <div className="sidebar-head">
        <span className="sidebar-label">{t("conversations")}</span>
        <button
          className="icon-btn small"
          id="btnNewChatSide"
          title={t("newConversation")}
          aria-label={t("newConversation")}
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
          placeholder={t("filterConversations")}
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
              {session.title || t("untitled")}
            </span>
            <span className="session-time">
              {relativeTime(session.updated_at, t)}
            </span>
            <button
              className="session-delete"
              type="button"
              title={t("deleteConversation")}
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
    </div>
  );
}
