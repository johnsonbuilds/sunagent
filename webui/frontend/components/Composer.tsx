"use client";

import { useEffect, useRef, useState, type RefObject } from "react";

import { useI18n } from "@/lib/i18n";
import type { HarnessOption, ModelOption } from "@/lib/types";

import { SendIcon, StopIcon } from "./Icons";

type PickerKind = "model" | "harness";

interface ComposerProps {
  value: string;
  busy: boolean;
  inputRef: RefObject<HTMLTextAreaElement | null>;
  modelId: string;
  modelLabel: string;
  harnessId: string;
  harnessLabel: string;
  harnessLocked: boolean;
  models: ModelOption[];
  harnesses: HarnessOption[];
  onChange: (value: string) => void;
  onSend: () => void;
  onStop: () => void;
  onModelChange: (id: string) => void;
  onHarnessChange: (id: string) => void;
}

export default function Composer({
  value,
  busy,
  inputRef,
  modelId,
  modelLabel,
  harnessId,
  harnessLabel,
  harnessLocked,
  models,
  harnesses,
  onChange,
  onSend,
  onStop,
  onModelChange,
  onHarnessChange,
}: ComposerProps) {
  const { t } = useI18n();
  const [open, setOpen] = useState<PickerKind | null>(null);
  const pickersRef = useRef<HTMLDivElement>(null);

  // Close on outside click / Escape. The menu floats over the transcript,
  // so it must not depend on the button keeping focus.
  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: PointerEvent) => {
      if (!pickersRef.current?.contains(event.target as Node)) setOpen(null);
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(null);
    };
    document.addEventListener("pointerdown", onPointerDown);
    document.addEventListener("keydown", onKeyDown);
    return () => {
      document.removeEventListener("pointerdown", onPointerDown);
      document.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  const toggle = (kind: PickerKind) => {
    setOpen((current) => (current === kind ? null : kind));
  };

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
          <div className="composer-left" ref={pickersRef}>
            <div className="picker">
              <button
                type="button"
                className="picker-btn"
                id="modelPicker"
                title={t("modelPickerTitle")}
                aria-haspopup="listbox"
                aria-expanded={open === "model"}
                onClick={() => toggle("model")}
              >
                <span className="picker-label">{modelLabel}</span>
                <span className="picker-caret">▾</span>
              </button>
              {open === "model" ? (
                <ul className="picker-menu" role="listbox">
                  {models.map((model) => (
                    <li key={model.id}>
                      <button
                        type="button"
                        className={`picker-item${
                          model.id === modelId ? " active" : ""
                        }`}
                        role="option"
                        aria-selected={model.id === modelId}
                        title={model.model_id}
                        onClick={() => {
                          setOpen(null);
                          if (model.id !== modelId) onModelChange(model.id);
                        }}
                      >
                        <span className="picker-item-label">
                          {model.label}
                        </span>
                        <span className="picker-item-meta">
                          {model.model_id}
                        </span>
                      </button>
                    </li>
                  ))}
                </ul>
              ) : null}
            </div>

            <div className="picker">
              <button
                type="button"
                className="picker-btn"
                id="harnessPicker"
                disabled={harnessLocked}
                title={
                  harnessLocked
                    ? t("harnessLockedHint")
                    : t("harnessPickerTitle")
                }
                aria-haspopup="listbox"
                aria-expanded={open === "harness"}
                onClick={() => {
                  if (!harnessLocked) toggle("harness");
                }}
              >
                <span className="picker-label">{harnessLabel}</span>
                <span className="picker-caret">
                  {harnessLocked ? "🔒" : "▾"}
                </span>
              </button>
              {open === "harness" && !harnessLocked ? (
                <ul className="picker-menu" role="listbox">
                  {harnesses.map((harness) => (
                    <li key={harness.id}>
                      <button
                        type="button"
                        className={`picker-item${
                          harness.id === harnessId ? " active" : ""
                        }`}
                        role="option"
                        aria-selected={harness.id === harnessId}
                        onClick={() => {
                          setOpen(null);
                          if (harness.id !== harnessId) {
                            onHarnessChange(harness.id);
                          }
                        }}
                      >
                        <span className="picker-item-label">
                          {harness.id}
                        </span>
                      </button>
                    </li>
                  ))}
                </ul>
              ) : null}
            </div>

            {busy ? (
              <span className="picker-note">{t("nextTurnHint")}</span>
            ) : null}
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
