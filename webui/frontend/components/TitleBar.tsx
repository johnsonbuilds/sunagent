"use client";

import { useI18n, type MessageKey } from "@/lib/i18n";
import type { StatusState } from "@/lib/ui";

import { FilesIcon, FolderOpenIcon, MenuIcon, PlusIcon, ThemeIcon } from "./Icons";

export type { StatusState } from "@/lib/ui";

interface TitleBarProps {
  title: string;
  status: StatusState | null;
  newActionTitle?: MessageKey;
  newActionLabel?: MessageKey;
  onToggleSidebar: () => void;
  onToggleTheme: () => void;
  onNewChat?: () => void;
  onOpenFiles?: () => void;
  isFilesOpen?: boolean;
}

export default function TitleBar({
  title,
  status,
  newActionTitle,
  newActionLabel,
  onToggleSidebar,
  onToggleTheme,
  onNewChat,
  onOpenFiles,
  isFilesOpen,
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
        {onNewChat ? (
          <button
            className="icon-btn primary"
            id="btnNewChat"
            title={t(newActionTitle ?? "newConversationShortcut")}
            aria-label={t(newActionLabel ?? "newConversation")}
            onClick={onNewChat}
          >
            <PlusIcon />
          </button>
        ) : null}
        {onOpenFiles ? (
          <button
            className="icon-btn"
            id="btnOpenFiles"
            title={t("navFiles")}
            aria-label={t("navFiles")}
            onClick={onOpenFiles}
          >
            {isFilesOpen ? <FolderOpenIcon /> : <FilesIcon />}
          </button>
        ) : null}
      </div>
    </header>
  );
}
