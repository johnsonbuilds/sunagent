"use client";

import type { RefObject } from "react";

import { useI18n } from "@/lib/i18n";

import { SendIcon, StopIcon } from "./Icons";

interface ComposerProps {
  value: string;
  busy: boolean;
  inputRef: RefObject<HTMLTextAreaElement | null>;
  onChange: (value: string) => void;
  onSend: () => void;
  onStop: () => void;
}

export default function Composer({
  value,
  busy,
  inputRef,
  onChange,
  onSend,
  onStop,
}: ComposerProps) {
  const { t } = useI18n();

  return (
    <div className="composer-wrap">
      <div className="composer-box" id="composerBox">
        <textarea
          id="msgInput"
          ref={inputRef}
          rows={1}
          placeholder={t("composerPlaceholder")}
          spellCheck={false}
          value={value}
          onChange={(event) => onChange(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              onSend();
            }
          }}
        />
        <div className="composer-footer">
          <div className="composer-left">
            <span className="composer-hint" id="composerHint">
              {t("composerHint")}
            </span>
          </div>
          <div className="composer-right">
            <button
              className={`send-btn${busy ? " stop" : ""}`}
              id="btnSend"
              title={busy ? t("stop") : t("send")}
              aria-label={busy ? t("stop") : t("send")}
              onClick={busy ? onStop : onSend}
            >
              {busy ? <StopIcon /> : <SendIcon />}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
