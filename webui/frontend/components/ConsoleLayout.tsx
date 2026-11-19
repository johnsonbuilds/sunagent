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

export default function ConsoleLayout({ children }: { children: ReactNode }) {
  const pathname = usePathname();
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
