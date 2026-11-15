"use client";

import { usePathname } from "next/navigation";
import type { ReactNode } from "react";

import AppShell from "@/components/shell/AppShell";
import { matchRoute } from "@/lib/routes";
import { BoardProvider } from "@/lib/stores/board";
import { RuntimeConfigProvider } from "@/lib/stores/runtime-config";
import { SessionsProvider } from "@/lib/stores/sessions";
import { ThemeProvider } from "@/lib/stores/theme";
import { ToastProvider } from "@/lib/stores/toast";

/**
 * Providers and the frame live in the console layout, so navigating
 * between pages keeps the event stream, board snapshot and sidebar state.
 * Only the route page below `AppShell` is swapped.
 */
export default function ConsoleLayout({ children }: { children: ReactNode }) {
  const pathname = usePathname();
  // The board polls every 5s while it is visible, and backs off to 25s
  // on other pages without ever stopping.
  const fastPoll = matchRoute(pathname)?.id === "board";

  return (
    <ThemeProvider>
      <ToastProvider>
        <RuntimeConfigProvider>
          <SessionsProvider>
            <BoardProvider fastPoll={fastPoll}>
              <AppShell>{children}</AppShell>
            </BoardProvider>
          </SessionsProvider>
        </RuntimeConfigProvider>
      </ToastProvider>
    </ThemeProvider>
  );
}
