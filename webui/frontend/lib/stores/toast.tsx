"use client";

import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
  type ReactNode,
} from "react";

export type ToastType = "info" | "success" | "error";

interface ToastContextValue {
  showToast: (message: string, type?: ToastType) => void;
  hideToast: () => void;
}

const ToastContext = createContext<ToastContextValue | null>(null);

export function ToastProvider({ children }: { children: ReactNode }) {
  const [toast, setToast] = useState<{ message: string; type: ToastType } | null>(null);
  const toastRef = useRef<HTMLDivElement>(null);
  const timerRef = useRef<number | null>(null);

  const showToast = useCallback(
    (message: string, type: ToastType = "info") => {
      setToast({ message, type });
      if (timerRef.current !== null) {
        window.clearTimeout(timerRef.current);
      }
      timerRef.current = window.setTimeout(() => {
        setToast(null);
      }, 5000);
    },
    [],
  );

  const hideToast = useCallback(() => {
    if (timerRef.current !== null) {
      window.clearTimeout(timerRef.current);
      timerRef.current = null;
    }
    setToast(null);
  }, []);

  useEffect(() => {
    return () => {
      if (timerRef.current !== null) {
        window.clearTimeout(timerRef.current);
      }
    };
  }, []);

  useEffect(() => {
    const element = toastRef.current;
    if (!toast || !element) return;
    if (typeof element.showPopover !== "function") return;
    if (!element.matches(":popover-open")) {
      element.showPopover();
    }
  }, [toast]);

  // Modal <dialog>s are painted in the browser's top layer, out of reach of
  // z-index. The toast rides in that layer too (as a manual popover), but a
  // dialog opened afterwards stacks above it, so re-show the toast to move it
  // back to the top of the layer.
  useEffect(() => {
    const observer = new MutationObserver(() => {
      const element = toastRef.current;
      if (!element || typeof element.showPopover !== "function") return;
      if (!element.matches(":popover-open")) return;
      if (!document.querySelector("dialog[open]")) return;
      element.hidePopover();
      element.showPopover();
    });
    observer.observe(document.body, {
      subtree: true,
      attributes: true,
      attributeFilter: ["open"],
    });
    return () => observer.disconnect();
  }, []);

  const value = useMemo(
    () => ({ showToast, hideToast }),
    [showToast, hideToast],
  );

  return (
    <ToastContext.Provider value={value}>
      {children}
      {toast && (
        <div
          ref={toastRef}
          className={`toast toast--${toast.type}`}
          id="toast"
          popover="manual"
          role="status"
          aria-live="polite"
        >
          {toast.message}
        </div>
      )}
    </ToastContext.Provider>
  );
}

export function useToast(): ToastContextValue {
  const value = useContext(ToastContext);
  if (!value) throw new Error("useToast must be used inside ToastProvider");
  return value;
}
