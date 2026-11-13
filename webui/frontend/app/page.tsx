"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import Composer from "@/components/Composer";
import ConfirmDialog from "@/components/ConfirmDialog";
import type { PanelId } from "@/components/IconRail";
import Sidebar from "@/components/Sidebar";
import TitleBar, { type StatusState } from "@/components/TitleBar";
import Transcript from "@/components/Transcript";
import { api } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import type {
  DeltaFrame,
  Frame,
  HarnessOption,
  ModelOption,
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

/** The server's placeholder title for a session that has no messages yet. */
const NEW_SESSION_TITLE = "New conversation";

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
  const { t } = useI18n();
  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [blocks, setBlocks] = useState<Segment[]>([]);
  const [busy, setBusy] = useState(false);
  const [title, setTitle] = useState(NEW_SESSION_TITLE);
  const [status, setStatus] = useState<StatusState | null>(null);
  const [models, setModels] = useState<ModelOption[]>([]);
  const [harnesses, setHarnesses] = useState<HarnessOption[]>([]);
  const [activeModel, setActiveModel] = useState("");
  const [activeHarness, setActiveHarness] = useState("");
  const [harnessLocked, setHarnessLocked] = useState(false);
  const [filter, setFilter] = useState("");
  const [draft, setDraft] = useState("");
  const [activePanel, setActivePanel] = useState<PanelId>("sessions");
  const [sidebarOpen, setSidebarOpen] = useState(true);
  const [narrow, setNarrow] = useState(false);
  const [confirmRestartOpen, setConfirmRestartOpen] = useState(false);
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
  const defaultsRef = useRef({ model: "", harness: "" });
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
    setStatus(value ? { label: "statusRunning" } : null);
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
        setStatus({ label: "statusReconnecting", kind: "error" });
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
        setTitle(detail.title || NEW_SESSION_TITLE);
        setBlocks(detail.segments || []);
        setActiveModel(detail.model || defaultsRef.current.model);
        setActiveHarness(detail.harness || defaultsRef.current.harness);
        setHarnessLocked(Boolean(detail.harness_locked));
      } catch (error) {
        showToast(errorMessage(error));
        return;
      }

      connectStream(sessionId);
      scheduleScroll();
      // On narrow screens the sidebar floats over the transcript, so it
      // gets out of the way once a conversation is picked.
      if (window.innerWidth < 860) setSidebarOpen(false);
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
      const body = JSON.stringify({
        model: activeModel || defaultsRef.current.model || undefined,
        harness: activeHarness || defaultsRef.current.harness || undefined,
      });
      const session = await api<SessionSummary>("/api/sessions", {
        method: "POST",
        body,
      });
      await refreshSessions();
      await selectSession(session.id);
      inputRef.current?.focus();
    } catch (error) {
      showToast(errorMessage(error));
    }
  }, [activeHarness, activeModel, refreshSessions, selectSession, showToast]);

  const changeModel = useCallback(
    async (model: string) => {
      if (!activeId || model === activeModel) return;
      const previous = activeModel;
      setActiveModel(model);
      try {
        await api(`/api/sessions/${activeId}`, {
          method: "PATCH",
          body: JSON.stringify({ model }),
        });
      } catch (error) {
        setActiveModel(previous);
        showToast(errorMessage(error));
      }
    },
    [activeId, activeModel, showToast],
  );

  const changeHarness = useCallback(
    async (harness: string) => {
      if (!activeId || harness === activeHarness) return;
      const previous = activeHarness;
      setActiveHarness(harness);
      try {
        await api(`/api/sessions/${activeId}`, {
          method: "PATCH",
          body: JSON.stringify({ harness }),
        });
        setHarnessLocked(false);
      } catch (error) {
        setActiveHarness(previous);
        showToast(errorMessage(error));
      }
    },
    [activeHarness, activeId, showToast],
  );

  const renameSession = useCallback(
    async (session: SessionSummary) => {
      const value = window.prompt(
        t("conversationTitle"),
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
    [activeId, refreshSessions, showToast, t],
  );

  const deleteSession = useCallback(
    async (session: SessionSummary) => {
      if (
        !window.confirm(
          t("deleteConversationConfirm", {
            title: session.title || t("thisConversation"),
          }),
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
    [activeId, newSession, refreshSessions, selectSession, showToast, t],
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
        // The first message fixes the harness for this conversation.
        setHarnessLocked(true);
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
        setConfirmRestartOpen(false);
        showToast(t("restartTimeoutToast"));
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
    [showToast, t],
  );

  const restartServer = useCallback(async () => {
    if (restarting) return;
    setRestarting(true);
    showToast(t("restartingToast"));
    try {
      await api("/api/system/restart", { method: "POST", body: "{}" });
    } catch (error) {
      setRestarting(false);
      setConfirmRestartOpen(false);
      showToast(errorMessage(error));
      return;
    }
    // Hand off to run.sh, then poll until the replacement server is
    // healthy and reload the page against it.
    restartTimerRef.current = window.setTimeout(() => {
      pollForRestart(Date.now() + 60000);
    }, 2000);
  }, [pollForRestart, restarting, showToast, t]);

  const requestRestart = useCallback(() => {
    setSidebarOpen(false);
    setConfirmRestartOpen(true);
  }, []);

  const selectPanel = useCallback((id: PanelId) => {
    setActivePanel(id);
    setSidebarOpen(true);
  }, []);

  const handleFrame = (frame: Frame) => {
    if (typeof frame.seq === "number") {
      if (frame.seq <= lastSeqRef.current) return;
      lastSeqRef.current = frame.seq;
    }

    switch (frame.type) {
      case "run.started":
        updateBusy(true);
        setHarnessLocked(true);
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
        setStatus({ label: "statusError", kind: "error" });
        showToast(frame.error || t("runFailed"));
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
    if (window.innerWidth < 860) setSidebarOpen(false);
    window.addEventListener("resize", sync);
    return () => window.removeEventListener("resize", sync);
  }, []);

  useEffect(() => {
    if (bootedRef.current) return;
    bootedRef.current = true;

    void (async () => {
      try {
        const config = await api<RuntimeConfig>("/api/config");
        const modelList = config.models || [];
        const harnessList = config.harnesses || [];
        const defaultModel = config.default_model || modelList[0]?.id || "";
        const defaultHarness =
          config.default_harness || harnessList[0]?.id || "";
        setModels(modelList);
        setHarnesses(harnessList);
        setActiveModel(defaultModel);
        setActiveHarness(defaultHarness);
        defaultsRef.current = {
          model: defaultModel,
          harness: defaultHarness,
        };
        if (config.error) showToast(config.error);
      } catch {
        setModels([]);
        setHarnesses([]);
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
    };
    document.addEventListener("keydown", onKeyDown);
    return () => document.removeEventListener("keydown", onKeyDown);
  }, [newSession]);

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

  const modelLabel =
    models.find((model) => model.id === activeModel)?.label ||
    activeModel;
  const modelChip = [modelLabel, activeHarness].filter(Boolean).join(" · ");
  const modelTitle = t("modelHarnessTitle", {
    model: modelLabel || "?",
    harness: activeHarness || "?",
  });
  const displayTitle =
    title === NEW_SESSION_TITLE ? t("newConversation") : title;

  return (
    <div className="app">
      <TitleBar
        title={displayTitle}
        status={status}
        modelChip={modelChip}
        modelTitle={modelTitle}
        onToggleSidebar={() => setSidebarOpen((value) => !value)}
        onToggleTheme={toggleTheme}
        onNewChat={() => void newSession()}
      />

      <div className="layout">
        <Sidebar
          sessions={sessions}
          activeId={activeId}
          filter={filter}
          activePanel={activePanel}
          collapsed={!sidebarOpen}
          backdropVisible={narrow && sidebarOpen}
          onSelectPanel={selectPanel}
          onFilterChange={setFilter}
          onSelect={(id) => void selectSession(id)}
          onNewChat={() => void newSession()}
          onDelete={(session) => void deleteSession(session)}
          onRename={(session) => void renameSession(session)}
          onRestart={requestRestart}
          onBackdropClick={() => setSidebarOpen(false)}
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
            modelId={activeModel}
            modelLabel={modelLabel || "—"}
            harnessId={activeHarness}
            harnessLabel={activeHarness || "—"}
            harnessLocked={harnessLocked}
            models={models}
            harnesses={harnesses}
            onChange={setDraft}
            onSend={() => void send()}
            onStop={() => void stop()}
            onModelChange={(id) => void changeModel(id)}
            onHarnessChange={(id) => void changeHarness(id)}
          />
        </main>
      </div>

      <ConfirmDialog
        open={confirmRestartOpen}
        title={t("restartDialogTitle")}
        message={t("restartDialogMessage")}
        confirmText={t("restartConfirm")}
        cancelText={t("defaultCancel")}
        busyText={t("restartBusy")}
        tone="danger"
        busy={restarting}
        onConfirm={() => void restartServer()}
        onCancel={() => setConfirmRestartOpen(false)}
      />

      <div className="toast" id="toast" hidden={!toast}>
        {toast}
      </div>
    </div>
  );
}
