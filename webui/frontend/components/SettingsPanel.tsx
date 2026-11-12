"use client";

import { RestartIcon } from "./Icons";

interface SettingsPanelProps {
  onRestart: () => void;
}

export default function SettingsPanel({ onRestart }: SettingsPanelProps) {
  return (
    <div className="panel">
      <div className="sidebar-head">
        <span className="sidebar-label">Settings</span>
      </div>
      <div className="settings-body">
        <button
          className="settings-item"
          id="btnRestart"
          type="button"
          onClick={onRestart}
        >
          <RestartIcon />
          <span className="settings-item-text">
            <span className="settings-item-title">重启</span>
            <span className="settings-item-desc">
              在当前目录执行 ./run.sh restart
            </span>
          </span>
        </button>
      </div>
    </div>
  );
}
