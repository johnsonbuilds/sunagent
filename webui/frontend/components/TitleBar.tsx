"use client";

import { BrandMark, MenuIcon, PlusIcon, ThemeIcon } from "./Icons";

export interface StatusState {
  text: string;
  kind?: "error";
}

interface TitleBarProps {
  title: string;
  status: StatusState | null;
  modelChip: string;
  modelTitle: string;
  onToggleSidebar: () => void;
  onToggleTheme: () => void;
  onNewChat: () => void;
}

export default function TitleBar({
  title,
  status,
  modelChip,
  modelTitle,
  onToggleSidebar,
  onToggleTheme,
  onNewChat,
}: TitleBarProps) {
  return (
    <header className="titlebar">
      <div className="titlebar-left">
        <button
          className="icon-btn"
          id="btnSidebar"
          title="Toggle sidebar"
          aria-label="Toggle sidebar"
          onClick={onToggleSidebar}
        >
          <MenuIcon />
        </button>
        <span className="brand">
          <BrandMark />
          <span className="brand-name">SunAgent</span>
        </span>
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
          {status?.text}
        </span>
      </div>

      <div className="titlebar-right">
        <span className="meta-chip" id="modelChip" title={modelTitle}>
          {modelChip}
        </span>
        <button
          className="icon-btn"
          id="btnTheme"
          title="Toggle theme"
          aria-label="Toggle theme"
          onClick={onToggleTheme}
        >
          <ThemeIcon />
        </button>
        <button
          className="icon-btn primary"
          id="btnNewChat"
          title="New conversation (Ctrl+K)"
          aria-label="New conversation"
          onClick={onNewChat}
        >
          <PlusIcon />
        </button>
      </div>
    </header>
  );
}
