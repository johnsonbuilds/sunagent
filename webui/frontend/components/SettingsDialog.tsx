"use client";

import { useEffect, useRef } from "react";

import { CloseIcon, RestartIcon } from "./Icons";

interface SettingsDialogProps {
  open: boolean;
  restarting: boolean;
  onClose: () => void;
  onRestart: () => void;
}

export default function SettingsDialog({
  open,
  restarting,
  onClose,
  onRestart,
}: SettingsDialogProps) {
  const closeRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (open) closeRef.current?.focus();
  }, [open]);

  return (
    <div
      className="settings-overlay"
      id="settingsOverlay"
      hidden={!open}
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
    >
      <div
        className="settings-panel"
        role="dialog"
        aria-modal="true"
        aria-labelledby="settingsTitle"
      >
        <div className="settings-head">
          <h2 className="settings-title" id="settingsTitle">
            Settings
          </h2>
          <button
            className="icon-btn small"
            id="btnSettingsClose"
            ref={closeRef}
            title="Close"
            aria-label="Close settings"
            onClick={onClose}
          >
            <CloseIcon size={15} />
          </button>
        </div>
        <div className="settings-body">
          <button
            className="settings-item"
            id="btnRestart"
            type="button"
            disabled={restarting}
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
    </div>
  );
}
