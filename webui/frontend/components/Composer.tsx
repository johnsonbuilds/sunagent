"use client";

import type { RefObject } from "react";

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
  return (
    <div className="composer-wrap">
      <div className="composer-box" id="composerBox">
        <textarea
          id="msgInput"
          ref={inputRef}
          rows={1}
          placeholder="Message SunAgent…"
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
              Enter sends · Shift+Enter adds a line
            </span>
          </div>
          <div className="composer-right">
            <button
              className={`send-btn${busy ? " stop" : ""}`}
              id="btnSend"
              title={busy ? "Stop" : "Send"}
              aria-label={busy ? "Stop" : "Send"}
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
