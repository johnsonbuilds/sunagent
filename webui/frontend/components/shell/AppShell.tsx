"use client";

import {
  usePathname,
  useRouter,
} from "next/navigation";
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

import Sidebar from "@/components/Sidebar";
import TitleBar from "@/components/TitleBar";
import { useI18n, type MessageKey } from "@/lib/i18n";
import { matchRoute } from "@/lib/routes";
import { useBoardStore } from "@/lib/stores/board";
import { useSessions } from "@/lib/stores/sessions";
import { useTheme } from "@/lib/stores/theme";
import type { StatusState } from "@/lib/ui";

interface ShellContextValue {
  narrow: boolean;
  collapsed: boolean;
  closeSidebar: () => void;
  openSidebar: () => void;
  toggleSidebar: () => void;
}

const ShellContext = createContext<ShellContextValue | null>(null);

function searchSessionId(): string | null {
  return new URLSearchParams(window.location.search).get("session");
}

function chatHref(sessionId: string | null): string {
  return sessionId ? `/?session=${encodeURIComponent(sessionId)}` : "/";
}

/**
 * The persistent frame: title bar, rail, sidebar and main area. The
 * current route is passed as children, so switching pages never rebuilds
 * the shell or the providers above it.
 */
export default function AppShell({ children }: { children: ReactNode }) {
  const { t } = useI18n();
  const { toggle: toggleTheme } = useTheme();
  const sessions = useSessions();
  const board = useBoardStore();
  const pathname = usePathname();
  const router = useRouter();

  const [collapsed, setCollapsed] = useState(false);
  const [narrow, setNarrow] = useState(false);

  const route = matchRoute(pathname);
  const isChat = route?.id === "chat";
  const title =
    isChat && sessions.displayTitle
      ? sessions.displayTitle
      : route?.titleKey
        ? t(route.titleKey)
        : "SunAgent";
  const status: StatusState | null = isChat ? sessions.status : null;

  let primaryTitle: MessageKey | undefined;
  let primaryLabel: MessageKey | undefined;
  let onPrimary: (() => void) | undefined;
  if (route?.primary === "newChat") {
    primaryTitle = "newConversationShortcut";
    primaryLabel = "newConversation";
    onPrimary = () => void sessions.newSession();
  } else if (route?.primary === "newTask") {
    primaryTitle = "boardNewTask";
    primaryLabel = "boardNewTask";
    onPrimary = () => board.openCreateTask("backlog");
  }

  // URL <-> selected conversation. The router owns page navigation; this
  // only keeps `?session=` in step with the active conversation.
  const activeIdRef = useRef(sessions.activeId);
  activeIdRef.current = sessions.activeId;
  const pathnameRef = useRef(pathname);
  pathnameRef.current = pathname;
  const sessionsRef = useRef(sessions);
  sessionsRef.current = sessions;
  const lastPathRef = useRef(pathname);
  const skipUrlSyncRef = useRef(false);

  const recoverSelection = useCallback(() => {
    const current = sessionsRef.current;
    if (current.sessions.length) {
      void current.selectSession(current.sessions[0].id);
    } else {
      void current.newSession();
    }
  }, []);

  useEffect(() => {
    const sync = () => {
      setNarrow(window.innerWidth < 860);
    };
    sync();
    if (window.innerWidth < 860) setCollapsed(true);
    window.addEventListener("resize", sync);
    return () => window.removeEventListener("resize", sync);
  }, []);

  useEffect(() => {
    const desired = title ? `${title} · SunAgent` : "SunAgent";
    const apply = () => {
      if (document.title !== desired) document.title = desired;
    };
    apply();
    // Next's metadata manager can rewrite the title during hydration or a
    // page transition; keep the route/session title authoritative.
    const observer = new MutationObserver(apply);
    observer.observe(document.head, {
      childList: true,
      subtree: true,
      characterData: true,
    });
    return () => observer.disconnect();
  }, [title]);

  const primaryRef = useRef(onPrimary);
  primaryRef.current = onPrimary;
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if (!primaryRef.current) return;
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        primaryRef.current();
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, []);

  // Entering the chat page with a conversation in the URL selects it.
  useEffect(() => {
    if (lastPathRef.current === pathname) return;
    lastPathRef.current = pathname;
    if (pathname !== "/") return;

    const desired = searchSessionId();
    if (!desired || desired === activeIdRef.current) return;
    skipUrlSyncRef.current = true;
    void sessions.selectSession(desired).then((ok) => {
      skipUrlSyncRef.current = false;
      if (!ok) recoverSelection();
    });
  }, [pathname, sessions.selectSession, recoverSelection]);

  // Selecting a conversation rewrites the query without adding history
  // entries, so the back button does not walk through every session.
  useEffect(() => {
    if (pathname !== "/" || !sessions.activeId) return;
    if (skipUrlSyncRef.current) {
      skipUrlSyncRef.current = false;
      return;
    }
    if (searchSessionId() === sessions.activeId) return;
    router.replace(chatHref(sessions.activeId), { scroll: false });
  }, [pathname, sessions.activeId, router]);

  // Browser back/forward between two chat URLs changes only the query, so
  // the pathname effect above does not run.
  useEffect(() => {
    const onPopState = () => {
      if (pathnameRef.current !== "/") return;
      if (window.location.pathname !== "/") return;
      const desired = searchSessionId();
      if (!desired || desired === activeIdRef.current) return;
      skipUrlSyncRef.current = true;
      void sessions.selectSession(desired).then((ok) => {
        skipUrlSyncRef.current = false;
        if (!ok) recoverSelection();
      });
    };
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, [sessions.selectSession, recoverSelection]);

  const value = useMemo<ShellContextValue>(
    () => ({
      narrow,
      collapsed,
      closeSidebar: () => setCollapsed(true),
      openSidebar: () => setCollapsed(false),
      toggleSidebar: () => setCollapsed((current) => !current),
    }),
    [narrow, collapsed],
  );

  return (
    <ShellContext.Provider value={value}>
      <div className="app">
        <TitleBar
          title={title}
          status={status}
          newActionTitle={primaryTitle}
          newActionLabel={primaryLabel}
          onToggleSidebar={value.toggleSidebar}
          onToggleTheme={toggleTheme}
          onNewChat={onPrimary}
        />

        <div className="layout">
          <Sidebar
            collapsed={collapsed}
            backdropVisible={narrow && !collapsed}
            onNavigate={() => setCollapsed(true)}
            onBackdropClick={value.closeSidebar}
          />

          <main
            className={`main${route?.id === "board" ? " main-board" : ""}${
              route?.id === "files" ? " main-files" : ""
            }`}
          >
            {children}
          </main>
        </div>
      </div>
    </ShellContext.Provider>
  );
}

export function useShell(): ShellContextValue {
  const value = useContext(ShellContext);
  if (!value) throw new Error("useShell must be used inside AppShell");
  return value;
}
