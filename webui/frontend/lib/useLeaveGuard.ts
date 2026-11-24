"use client";

import { useEffect } from "react";

import { setLeaveBlocker } from "./leaveGuard";

/** Marks a history entry this module pushed to catch the back gesture. */
const SENTINEL_KEY = "__sunagentLeaveGuard";

function pushSentinel(): void {
  window.history.pushState(
    { ...(window.history.state ?? {}), [SENTINEL_KEY]: true },
    "",
    window.location.href,
  );
}

function onSentinel(): boolean {
  const state: Record<string, unknown> | null = window.history.state;
  return Boolean(state?.[SENTINEL_KEY]);
}

/**
 * Keeps the page while `active`: the back gesture and the app's own
 * navigations are cancelled and `onBlocked` explains why; a reload or tab
 * close falls back to the browser's own confirmation. The sentinel history
 * entry is popped again as soon as the guard retires, so it never eats a back
 * press of its own.
 */
export function useLeaveGuard(active: boolean, onBlocked: () => void): void {
  useEffect(() => {
    if (!active) return;

    setLeaveBlocker(onBlocked);
    pushSentinel();

    // The browser already moved off the sentinel; re-push it so the page does
    // not change under the notice.
    const onPopState = () => {
      pushSentinel();
      onBlocked();
    };

    const onBeforeUnload = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      // Browsers show their own wording; the value only has to be set.
      event.returnValue = "";
    };

    window.addEventListener("popstate", onPopState);
    window.addEventListener("beforeunload", onBeforeUnload);
    return () => {
      setLeaveBlocker(null);
      window.removeEventListener("popstate", onPopState);
      window.removeEventListener("beforeunload", onBeforeUnload);
      if (onSentinel()) window.history.go(-1);
    };
  }, [active, onBlocked]);
}
