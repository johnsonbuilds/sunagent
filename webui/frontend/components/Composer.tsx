"use client";

import { useEffect, useRef, useState } from "react";

import { useI18n } from "@/lib/i18n";
import { useRuntimeConfig } from "@/lib/stores/runtime-config";
import { useDraft, useSessions } from "@/lib/stores/sessions";

import { SendIcon, StopIcon } from "./Icons";

type PickerKind = "model" | "harness";

export default function Composer() {
  const { t } = useI18n();
  const { models, harnesses } = useRuntimeConfig();
  const {
    activeId,
    busy,
    activeModel,
    activeHarness,
    harnessLocked,
    focusSeq,
    send,
    stop,
    changeModel,
    changeHarness,
  } = useSessions();
  const { draft, setDraft } = useDraft();

  const [open, setOpen] = useState<PickerKind | null>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const pickersRef = useRef<HTMLDivElement>(null);

  const modelLabel =
    models.find((model) => model.id === activeModel)?.label
    || activeModel
    || "—";
  const harnessLabel = activeHarness || "—";

  useEffect(() => {
    if (!focusSeq) return;
    inputRef.current?.focus();
  }, [focusSeq]);

  useEffect(() => {
    const element = inputRef.current;
    if (!element) return;
    element.style.height = "auto";
    element.style.height = `${Math.min(element.scrollHeight, 220)}px`;
  }, [draft]);

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

  const submit = () => {
    const text = draft.trim();
    if (!text || busy || !activeId) return;
    setDraft("");
    void send(text);
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
          value={draft}
          onChange={(event) => setDraft(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey) {
              event.preventDefault();
              submit();
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
                          model.id === activeModel ? " active" : ""
                        }`}
                        role="option"
                        aria-selected={model.id === activeModel}
                        title={model.model_id}
                        onClick={() => {
                          setOpen(null);
                          if (model.id !== activeModel) {
                            void changeModel(model.id);
                          }
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
                          harness.id === activeHarness ? " active" : ""
                        }`}
                        role="option"
                        aria-selected={harness.id === activeHarness}
                        onClick={() => {
                          setOpen(null);
                          if (harness.id !== activeHarness) {
                            void changeHarness(harness.id);
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
              onClick={busy ? () => void stop() : submit}
            >
              {busy ? <StopIcon /> : <SendIcon />}
            </button>
          </div>
        </div>
      </div>
    </div>
  );
}
