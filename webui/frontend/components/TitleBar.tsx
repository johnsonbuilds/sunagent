"use client";

import { useI18n, type MessageKey } from "@/lib/i18n";

import { MenuIcon, PlusIcon, ThemeIcon } from "./Icons";

export interface StatusState {
  label: MessageKey;
  kind?: "error";
}

interface TitleBarProps {
  title: string;
  status: StatusState | null;
  onToggleSidebar: () => void;
  onToggleTheme: () => void;
  onNewChat: () => void;
}

export default function TitleBar({
  title,
  status,
  onToggleSidebar,
  onToggleTheme,
  onNewChat,
}: TitleBarProps) {
  const { t } = useI18n();

  return (
    <header className="titlebar">
      <div className="titlebar-left">
        <button
          className="icon-btn"
          id="btnSidebar"
          title={t("toggleSidebar")}
          aria-label={t("toggleSidebar")}
          onClick={onToggleSidebar}
        >
          <MenuIcon />
        </button>
      </div>

      <div className="titlebar-center">
        <span className="titlebar-title" id="sessionTitle">
          {title}
        </span>
        <span
          className="status-badge"
          id="statusBadge"
          data-state={status?.kind}
          hidden={!status}
        >
          {status ? t(status.label) : null}
        </span>
      </div>

      <div className="titlebar-right">
        <button
          className="icon-btn"
          id="btnTheme"
          title={t("toggleTheme")}
          aria-label={t("toggleTheme")}
          onClick={onToggleTheme}
        >
          <ThemeIcon />
        </button>
        <button
          className="icon-btn primary"
          id="btnNewChat"
          title={t("newConversationShortcut")}
          aria-label={t("newConversation")}
          onClick={onNewChat}
        >
          <PlusIcon />
        </button>
      </div>
    </header>
  );
}
