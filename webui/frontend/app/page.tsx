"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import Composer from "@/components/Composer";
import SettingsDialog from "@/components/SettingsDialog";
import Sidebar from "@/components/Sidebar";
import TitleBar, { type StatusState } from "@/components/TitleBar";
import Transcript from "@/components/Transcript";
import { api } from "@/lib/api";
import type {
  DeltaFrame,
  Frame,
  RuntimeConfig,
  Segment,
  SessionDetail,
  SessionSummary,
  ToolFrame,
  ToolSegment,
} from "@/lib/types";

function errorMessage(error: unknown): string {
  return error instanceof Error ? error.message : String(error);
}

function upsertSegment(list: Segment[], segment: Segment): Segment[] {
  const index = list.findIndex((block) => block.id === segment.id);
  if (index === -1) return [...list, segment];
  const next = list.slice();
  next[index] = segment;
  return next;
}

function appendDelta(list: Segment[], frame: DeltaFrame): Segment[] {
  const index = list.findIndex((block) => block.id === frame.id);
  if (index === -1) {
    return [
      ...list,
      {
        id: frame.id,
        type: frame.channel,
        content: frame.text || "",
      },
    ];
  }
  const current = list[index];
  if (current.type === "tool") return list;
  const next = list.slice();
  next[index] = {
    ...current,
    content: (current.content || "") + (frame.text || ""),
  };
  return next;
}

function toolSegment(frame: ToolFrame): ToolSegment {
  return {
    id: frame.id,
    type: "tool",
    call_id: frame.call_id,
    tool: frame.tool,
    summary: frame.summary,
    arguments: frame.arguments,
    status: frame.status || "running",
    duration: frame.duration,
    exit_code: frame.exit_code,
    output: frame.output,
    error: frame.error,
  };
}

export default function Home() {
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [blocks, setBlocks] = useState<Segment[]>([]);
  const [busy, setBusy] = useState(false);
  const [title, setTitle] = useState("New conversation");
  const [status, setStatus] = useState<StatusState | null>(null);
  const [modelChip, setModelChip] = useState("");
  const [modelTitle, setModelTitle] = useState("Model and harness");
  const [filter, setFilter] = useState("");
  const [draft, setDraft] = useState("");
  const [collapsed, setCollapsed] = useState(false);
  const [narrow, setNarrow] = useState(false);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [restarting, setRestarting] = useState(false);
  const [toast, setToast] = useState("");

  const messagesRef = useRef<HTMLDivElement>(null);
  const inputRef = useRef<HTMLTextAreaElement>(null);
  const streamRef = useRef<EventSource | null>(null);
  const lastSeqRef = useRef(0);
  const pinnedRef = useRef(true);
  const scrollScheduledRef = useRef(false);
  const toastTimerRef = useRef<number | null>(null);
  const restartTimerRef = useRef<number | null>(null);
  const bootedRef = useRef(false);
  const handleFrameRef = useRef<(frame: Frame) => void>(() => {});

  const showToast = useCallback((message: string) => {
    setToast(message);
    if (toastTimerRef.current !== null) {
      window.clearTimeout(toastTimerRef.current);
    }
    toastTimerRef.current = window.setTimeout(() => {
      setToast("");
    }, 5000);
  }, []);

  const updateBusy = useCallback((value: boolean) => {
    setBusy(value);
    setStatus(value ? { text: "running" } : null);
  }, []);

  const refreshSessions = useCallback(async () => {
    const list = await api<SessionSummary[]>("/api/sessions");
    setSessions(list);
    return list;
  }, []);

  const scheduleScroll = useCallback(() => {
    if (scrollScheduledRef.current) return;
    scrollScheduledRef.current = true;
    window.requestAnimationFrame(() => {
      scrollScheduledRef.current = false;
      if (!pinnedRef.current) return;
      const element = messagesRef.current;
      if (element) element.scrollTop = element.scrollHeight;
    });
  }, []);

  const disconnectStream = useCallback(() => {
    if (streamRef.current) {
      streamRef.current.close();
      streamRef.current = null;
    }
  }, []);

  const connectStream = useCallback(
    (sessionId: string) => {
      disconnectStream();
      const source = new EventSource(
        `/api/sessions/${sessionId}/events`,
      );
      streamRef.current = source;
      source.onmessage = (event) => {
        let frame: Frame;
        try {
          frame = JSON.parse(event.data) as Frame;
        } catch {
          return;
        }
        handleFrameRef.current(frame);
      };
      source.onerror = () => {
        // The browser reconnects on its own and replays with
        // Last-Event-ID, so the stream recovers without user action.
        setStatus({ text: "reconnecting", kind: "error" });
      };
    },
    [disconnectStream],
  );

  const selectSession = useCallback(
    async (sessionId: string) => {
      disconnectStream();
      setActiveId(sessionId);
      lastSeqRef.current = 0;
      pinnedRef.current = true;
      setBlocks([]);
      updateBusy(false);

      try {
        const detail = await api<SessionDetail>(
          `/api/sessions/${sessionId}`,
        );
        setTitle(detail.title || "New conversation");
        setBlocks(detail.segments || []);
      } catch (error) {
        showToast(errorMessage(error));
        return;
      }

      connectStream(sessionId);
      scheduleScroll();
      // On narrow screens the sidebar floats over the transcript, so it
      // gets out of the way once a conversation is picked.
      if (window.innerWidth < 860) setCollapsed(true);
    },
    [
      connectStream,
      disconnectStream,
      scheduleScroll,
      showToast,
      updateBusy,
    ],
  );

  const newSession = useCallback(async () => {
    try {
      const session = await api<SessionSummary>("/api/sessions", {
        method: "POST",
        body: "{}",
      });
      await refreshSessions();
      await selectSession(session.id);
      inputRef.current?.focus();
    } catch (error) {
      showToast(errorMessage(error));
    }
  }, [refreshSessions, selectSession, showToast]);

  const renameSession = useCallback(
    async (session: SessionSummary) => {
      const value = window.prompt(
        "Conversation title",
        session.title || "",
      );
      if (value === null) return;
      try {
        await api(`/api/sessions/${session.id}`, {
          method: "PATCH",
          body: JSON.stringify({ title: value }),
        });
        await refreshSessions();
        if (session.id === activeId) {
          setTitle(value.trim() || session.title);
        }
      } catch (error) {
        showToast(errorMessage(error));
      }
    },
    [activeId, refreshSessions, showToast],
  );

  const deleteSession = useCallback(
    async (session: SessionSummary) => {
      if (
        !window.confirm(
          `Delete "${session.title || "this conversation"}"?`,
        )
      ) {
        return;
      }
      try {
        await api(`/api/sessions/${session.id}`, { method: "DELETE" });
        const list = await refreshSessions();
        if (session.id === activeId) {
          if (list.length) await selectSession(list[0].id);
          else await newSession();
        }
      } catch (error) {
        showToast(errorMessage(error));
      }
    },
    [activeId, newSession, refreshSessions, selectSession, showToast],
  );

  const send = useCallback(
    async (override?: string) => {
      const text = (override ?? draft).trim();
      if (!text || busy || !activeId) return;
      setDraft("");

      setBlocks((list) => [
        ...list,
        {
          id: `local-${Date.now()}-${Math.random().toString(16).slice(2)}`,
          type: "user",
          content: text,
        },
      ]);

      try {
        await api(`/api/sessions/${activeId}/messages`, {
          method: "POST",
          body: JSON.stringify({ message: text }),
        });
        updateBusy(true);
      } catch (error) {
        updateBusy(false);
        showToast(errorMessage(error));
      }
    },
    [activeId, busy, draft, showToast, updateBusy],
  );

  const stop = useCallback(async () => {
    if (!activeId) return;
    try {
      await api(`/api/sessions/${activeId}/cancel`, {
        method: "POST",
        body: "{}",
      });
    } catch (error) {
      showToast(errorMessage(error));
    }
  }, [activeId, showToast]);

  const toggleTheme = useCallback(() => {
    const current =
      document.documentElement.getAttribute("data-theme");
    const next = current === "dark" ? "light" : "dark";
    document.documentElement.setAttribute("data-theme", next);
    try {
      window.localStorage.setItem("sunagent-theme", next);
    } catch {
      /* private mode */
    }
  }, []);

  const pollForRestart = useCallback(
    (deadline: number) => {
      if (Date.now() > deadline) {
        setRestarting(false);
        showToast("服务重启超时，请手动刷新页面");
        return;
      }
      fetch("/api/health", { cache: "no-store" })
        .then((response) => {
          if (!response.ok) throw new Error("not ready");
          window.location.reload();
        })
        .catch(() => {
          restartTimerRef.current = window.setTimeout(() => {
            pollForRestart(deadline);
          }, 1000);
        });
    },
    [showToast],
  );

  const restartServer = useCallback(async () => {
    if (restarting) return;
    if (
      !window.confirm(
        "确定要重启 SunAgent 服务吗？当前页面会短暂断开连接。",
      )
    ) {
      return;
    }
    setRestarting(true);
    showToast("正在重启服务，请稍候…");
    try {
      await api("/api/system/restart", { method: "POST", body: "{}" });
    } catch (error) {
      setRestarting(false);
      showToast(errorMessage(error));
      return;
    }
    // Hand off to run.sh, then poll until the replacement server is
    // healthy and reload the page against it.
    restartTimerRef.current = window.setTimeout(() => {
      pollForRestart(Date.now() + 60000);
    }, 2000);
  }, [pollForRestart, restarting, showToast]);

  const handleFrame = (frame: Frame) => {
    if (typeof frame.seq === "number") {
      if (frame.seq <= lastSeqRef.current) return;
      lastSeqRef.current = frame.seq;
    }

    switch (frame.type) {
      case "run.started":
        updateBusy(true);
        break;
      case "delta":
        setBlocks((list) => appendDelta(list, frame));
        break;
      case "tool.started":
      case "tool.completed":
      case "tool.failed":
        setBlocks((list) => upsertSegment(list, toolSegment(frame)));
        break;
      case "segment":
        setBlocks((list) => upsertSegment(list, frame.segment));
        break;
      case "run.completed":
        updateBusy(false);
        void refreshSessions().catch(() => {});
        break;
      case "run.error":
        updateBusy(false);
        setStatus({ text: "error", kind: "error" });
        showToast(frame.error || "The run failed");
        void refreshSessions().catch(() => {});
        break;
      default:
        break;
    }
  };
  handleFrameRef.current = handleFrame;

  useEffect(() => {
    const element = inputRef.current;
    if (!element) return;
    element.style.height = "auto";
    element.style.height = `${Math.min(element.scrollHeight, 220)}px`;
  }, [draft]);

  useEffect(() => {
    scheduleScroll();
  }, [blocks, scheduleScroll]);

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
    if (bootedRef.current) return;
    bootedRef.current = true;

    void (async () => {
      try {
        const config = await api<RuntimeConfig>("/api/config");
        const parts: string[] = [];
        if (config.model) parts.push(config.model);
        if (config.harness) parts.push(config.harness);
        setModelChip(parts.join(" · "));
        setModelTitle(
          `model ${config.model || "?"} · harness ${config.harness || "?"}`,
        );
        if (config.error) showToast(config.error);
      } catch {
        setModelChip("");
      }

      try {
        const list = await refreshSessions();
        if (!list.length) await newSession();
        else await selectSession(list[0].id);
      } catch (error) {
        showToast(errorMessage(error));
      }
    })();
  }, [newSession, refreshSessions, selectSession, showToast]);

  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "k") {
        event.preventDefault();
        void newSession();
      }
      if (event.key === "Escape" && settingsOpen) {
        setSettingsOpen(false);
      }
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [newSession, settingsOpen]);

  useEffect(() => {
    return () => {
      disconnectStream();
      if (toastTimerRef.current !== null) {
        window.clearTimeout(toastTimerRef.current);
      }
      if (restartTimerRef.current !== null) {
        window.clearTimeout(restartTimerRef.current);
      }
    };
  }, [disconnectStream]);

  return (
    <div className="app">
      <TitleBar
        title={title}
        status={status}
        modelChip={modelChip}
        modelTitle={modelTitle}
        onToggleSidebar={() => setCollapsed((value) => !value)}
        onToggleTheme={toggleTheme}
        onNewChat={() => void newSession()}
      />

      <div className="layout">
        <Sidebar
          sessions={sessions}
          activeId={activeId}
          filter={filter}
          collapsed={collapsed}
          backdropVisible={narrow && !collapsed}
          onFilterChange={setFilter}
          onSelect={(id) => void selectSession(id)}
          onNewChat={() => void newSession()}
          onDelete={(session) => void deleteSession(session)}
          onRename={(session) => void renameSession(session)}
          onOpenSettings={() => setSettingsOpen(true)}
          onBackdropClick={() => setCollapsed(true)}
        />

        <main className="main">
          <Transcript
            blocks={blocks}
            messagesRef={messagesRef}
            onSuggestion={(text) => void send(text)}
            onScroll={() => {
              const element = messagesRef.current;
              if (!element) return;
              const distance =
                element.scrollHeight -
                element.scrollTop -
                element.clientHeight;
              pinnedRef.current = distance < 120;
            }}
          />

          <Composer
            value={draft}
            busy={busy}
            inputRef={inputRef}
            onChange={setDraft}
            onSend={() => void send()}
            onStop={() => void stop()}
          />
        </main>
      </div>

      <SettingsDialog
        open={settingsOpen}
        restarting={restarting}
        onClose={() => setSettingsOpen(false)}
        onRestart={() => void restartServer()}
      />

      <div className="toast" id="toast" hidden={!toast}>
        {toast}
      </div>
    </div>
  );
}
