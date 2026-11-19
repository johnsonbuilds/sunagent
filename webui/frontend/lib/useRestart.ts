"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import { api } from "@/lib/api";
import { useI18n } from "@/lib/i18n";

import { useToast } from "./stores/toast";

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

export function useRestart() {
  const { t } = useI18n();
  const { showToast } = useToast();
  const [open, setOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const timerRef = useRef<number | null>(null);

  const pollForRestart = useCallback(
    (deadline: number) => {
      if (Date.now() > deadline) {
        setBusy(false);
        setOpen(false);
        showToast(t("restartTimeoutToast"));
        return;
      }
      fetch("/api/health", { cache: "no-store" })
        .then((response) => {
          if (!response.ok) throw new Error("not ready");
          window.location.reload();
        })
        .catch(() => {
          timerRef.current = window.setTimeout(() => {
            pollForRestart(deadline);
          }, 1000);
        });
    },
    [showToast, t],
  );

  const confirm = useCallback(async () => {
    if (busy) return;
    setBusy(true);
    showToast(t("restartingToast"));
    try {
      await api("/api/system/restart", { method: "POST", body: "{}" });
    } catch (error) {
      setBusy(false);
      setOpen(false);
      showToast(errorMessage(error));
      return;
    }
    timerRef.current = window.setTimeout(() => {
      pollForRestart(Date.now() + 60000);
    }, 2000);
  }, [busy, pollForRestart, showToast, t]);

  useEffect(() => {
    return () => {
      if (timerRef.current !== null) {
        window.clearTimeout(timerRef.current);
      }
    };
  }, []);

  return {
    open,
    busy,
    request: () => setOpen(true),
    cancel: () => {
      if (!busy) setOpen(false);
    },
    confirm,
  };
}
