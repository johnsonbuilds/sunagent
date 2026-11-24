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

import { api } from "@/lib/api";
import { useI18n } from "@/lib/i18n";
import type {
  DeltaFrame,
  Frame,
  Segment,
  SessionDetail,
  SessionSummary,
  ToolFrame,
  ToolSegment,
} from "@/lib/types";
import type { StatusState } from "@/lib/ui";

import { useRuntimeConfig } from "./runtime-config";
import { useToast } from "./toast";

const NEW_SESSION_TITLE = "New conversation";
const DRAFT_KEY_PREFIX = "sunagent-draft:";

/** Composer text is kept per conversation so it survives navigation and reload. */
function readDraft(sessionId: string): string {
  try {
    return window.localStorage.getItem(DRAFT_KEY_PREFIX + sessionId) ?? "";
  } catch {
    return "";
  }
}

function writeDraft(sessionId: string, value: string): void {
  try {
    if (value) {
      window.localStorage.setItem(DRAFT_KEY_PREFIX + sessionId, value);
    } else {
      window.localStorage.removeItem(DRAFT_KEY_PREFIX + sessionId);
    }
  } catch {
    /* private mode */
  }
}

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

export interface SessionsValue {
  sessions: SessionSummary[];
  activeId: string | null;
  title: string;
  displayTitle: string;
  status: StatusState | null;
  busy: boolean;
  filter: string;
  setFilter: (value: string) => void;
  activeModel: string;
  activeHarness: string;
  harnessLocked: boolean;
  boardSyncSeq: number;
  focusSeq: number;
  refreshSessions: () => Promise<SessionSummary[]>;
  /** Resolves false when the session is gone or the request lost a race. */
  selectSession: (sessionId: string) => Promise<boolean>;
  newSession: () => Promise<void>;
  changeModel: (model: string) => Promise<void>;
  changeHarness: (harness: string) => Promise<void>;
  renameSession: (session: SessionSummary) => Promise<void>;
  deleteSession: (session: SessionSummary) => Promise<void>;
  /** Copy the conversation up to one answer into a new, independent one. */
  branchFrom: (segmentId: string) => Promise<void>;
  send: (text: string) => Promise<void>;
  stop: () => Promise<void>;
}

interface DraftValue {
  draft: string;
  setDraft: (value: string) => void;
}

const SessionsContext = createContext<SessionsValue | null>(null);
const SegmentsContext = createContext<Segment[] | null>(null);
const DraftContext = createContext<DraftValue | null>(null);

export function SessionsProvider({ children }: { children: ReactNode }) {
  const { t } = useI18n();
  const { showToast } = useToast();
  const config = useRuntimeConfig();

  const [sessions, setSessions] = useState<SessionSummary[]>([]);
  const [activeId, setActiveId] = useState<string | null>(null);
  const [segments, setSegments] = useState<Segment[]>([]);
  const [busy, setBusy] = useState(false);
  const [title, setTitle] = useState(NEW_SESSION_TITLE);
  const [status, setStatus] = useState<StatusState | null>(null);
  const [filter, setFilter] = useState("");
  const [activeModel, setActiveModel] = useState("");
  const [activeHarness, setActiveHarness] = useState("");
  const [harnessLocked, setHarnessLocked] = useState(false);
  const [boardSyncSeq, setBoardSyncSeq] = useState(0);
  const [focusSeq, setFocusSeq] = useState(0);
  const [draft, setDraftState] = useState("");
  const draftSessionRef = useRef<string | null>(null);

  const streamRef = useRef<EventSource | null>(null);
  const lastSeqRef = useRef(0);
  const bootedRef = useRef(false);
  const selectionRef = useRef(0);
  const handleFrameRef = useRef<(frame: Frame) => void>(() => {});

  useEffect(() => {
    if (!config.loaded) return;
    setActiveModel((current) => current || config.defaultModel);
    setActiveHarness((current) => current || config.defaultHarness);
  }, [config.loaded, config.defaultModel, config.defaultHarness]);

  useEffect(() => {
    if (draftSessionRef.current === activeId) return;
    draftSessionRef.current = activeId;
    setDraftState(activeId ? readDraft(activeId) : "");
  }, [activeId]);

  const setDraft = useCallback((value: string) => {
    setDraftState(value);
    const sessionId = draftSessionRef.current;
    if (sessionId) writeDraft(sessionId, value);
  }, []);

  const bumpBoardSync = useCallback(() => {
    setBoardSyncSeq((value) => value + 1);
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
    async (sessionId: string): Promise<boolean> => {
      const token = ++selectionRef.current;
      disconnectStream();
      setActiveId(sessionId);
      lastSeqRef.current = 0;
      setSegments([]);
      updateBusy(false);

      let detail: SessionDetail;
      try {
        detail = await api<SessionDetail>(`/api/sessions/${sessionId}`);
      } catch (error) {
        if (token !== selectionRef.current) return false;
        showToast(errorMessage(error), "error");
        return false;
      }
      if (token !== selectionRef.current) return false;

      setTitle(detail.title || NEW_SESSION_TITLE);
      setSegments(detail.segments || []);
      setActiveModel(detail.model || config.defaultModel);
      setActiveHarness(detail.harness || config.defaultHarness);
      setHarnessLocked(Boolean(detail.harness_locked));
      // A turn may already be running (page reload, board task, another
      // tab): start from the server's answer so Stop is reachable and the
      // composer does not accept a second message.
      updateBusy(Boolean(detail.busy));
      connectStream(sessionId);
      return true;
    },
    [
      config.defaultHarness,
      config.defaultModel,
      connectStream,
      disconnectStream,
      showToast,
      updateBusy,
    ],
  );

  const newSession = useCallback(async () => {
    try {
      // A new conversation starts from the service defaults (the
      // model.json default entry and the startup harness), never from
      // the conversation that happens to be selected right now.
      const session = await api<SessionSummary>("/api/sessions", {
        method: "POST",
        body: "{}",
      });
      await refreshSessions();
      await selectSession(session.id);
      setFocusSeq((value) => value + 1);
    } catch (error) {
      showToast(errorMessage(error), "error");
    }
  }, [refreshSessions, selectSession, showToast]);

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
        // Keep the list summary in sync so the details dialog, which reads
        // from it, never shows the previous model.
        setSessions((list) =>
          list.map((item) =>
            item.id === activeId ? { ...item, model } : item,
          ),
        );
      } catch (error) {
        setActiveModel(previous);
        showToast(errorMessage(error), "error");
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
        setSessions((list) =>
          list.map((item) =>
            item.id === activeId ? { ...item, harness } : item,
          ),
        );
        setHarnessLocked(false);
      } catch (error) {
        setActiveHarness(previous);
        showToast(errorMessage(error), "error");
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
        showToast(errorMessage(error), "error");
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
        writeDraft(session.id, "");
        const list = await refreshSessions();
        // The server unlinks a deleted conversation from its task.
        bumpBoardSync();
        if (session.id === activeId) {
          if (list.length) await selectSession(list[0].id);
          else await newSession();
        }
      } catch (error) {
        showToast(errorMessage(error), "error");
      }
    },
    [
      activeId,
      bumpBoardSync,
      newSession,
      refreshSessions,
      selectSession,
      showToast,
      t,
    ],
  );

  const send = useCallback(
    async (text: string) => {
      const value = text.trim();
      if (!value || busy || !activeId) return;

      setSegments((list) => [
        ...list,
        {
          id: `local-${Date.now()}-${Math.random().toString(16).slice(2)}`,
          type: "user",
          content: value,
        },
      ]);

      try {
        await api(`/api/sessions/${activeId}/messages`, {
          method: "POST",
          body: JSON.stringify({ message: value }),
        });
        updateBusy(true);
        setHarnessLocked(true);
        bumpBoardSync();
      } catch (error) {
        updateBusy(false);
        showToast(errorMessage(error), "error");
      }
    },
    [activeId, busy, bumpBoardSync, showToast, updateBusy],
  );

  const stop = useCallback(async () => {
    if (!activeId) return;
    try {
      await api(`/api/sessions/${activeId}/cancel`, {
        method: "POST",
        body: "{}",
      });
    } catch (error) {
      showToast(errorMessage(error), "error");
    }
  }, [activeId, showToast]);

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
        setSegments((list) => appendDelta(list, frame));
        break;
      case "tool.started":
      case "tool.completed":
      case "tool.failed":
        setSegments((list) => upsertSegment(list, toolSegment(frame)));
        break;
      case "segment":
        setSegments((list) => upsertSegment(list, frame.segment));
        break;
      case "run.completed":
        updateBusy(false);
        void refreshSessions().catch(() => {});
        bumpBoardSync();
        break;
      case "run.error":
        updateBusy(false);
        setStatus({ label: "statusError", kind: "error" });
        showToast(frame.error || t("runFailed"), "error");
        void refreshSessions().catch(() => {});
        bumpBoardSync();
        break;
      default:
        break;
    }
  };
  handleFrameRef.current = handleFrame;

  useEffect(() => {
    if (!config.loaded || bootedRef.current) return;
    bootedRef.current = true;

    void (async () => {
      try {
        const requested = new URLSearchParams(window.location.search).get(
          "session",
        );
        const list = await refreshSessions();
        const known =
          requested !== null && list.some((item) => item.id === requested);

        if (requested && !known) showToast(t("conversationNotFound"), "error");
        if (known && requested) await selectSession(requested);
        else if (list.length) await selectSession(list[0].id);
        else await newSession();
      } catch (error) {
        showToast(errorMessage(error), "error");
      }
    })();
  }, [
    config.loaded,
    newSession,
    refreshSessions,
    selectSession,
    showToast,
    t,
  ]);

  useEffect(() => {
    return () => {
      disconnectStream();
    };
  }, [disconnectStream]);

  const displayTitle =
    title === NEW_SESSION_TITLE ? t("newConversation") : title;

  const branchFrom = useCallback(
    async (segmentId: string) => {
      if (!activeId) return;
      try {
        const child = await api<SessionDetail>(
          `/api/sessions/${activeId}/branch`,
          {
            method: "POST",
            body: JSON.stringify({
              segment_id: segmentId,
              title: `${displayTitle}${t("branchTitleSuffix")}`,
            }),
          },
        );
        await refreshSessions();
        await selectSession(child.id);
        setFocusSeq((value) => value + 1);
        showToast(t("branchCreated"), "success");
      } catch (error) {
        showToast(errorMessage(error), "error");
      }
    },
    [activeId, displayTitle, refreshSessions, selectSession, showToast, t],
  );

  const value = useMemo<SessionsValue>(
    () => ({
      sessions,
      activeId,
      title,
      displayTitle,
      status,
      busy,
      filter,
      setFilter,
      activeModel,
      activeHarness,
      harnessLocked,
      boardSyncSeq,
      focusSeq,
      refreshSessions,
      selectSession,
      newSession,
      changeModel,
      changeHarness,
      renameSession,
      deleteSession,
      branchFrom,
      send,
      stop,
    }),
    [
      sessions,
      activeId,
      title,
      displayTitle,
      status,
      busy,
      filter,
      activeModel,
      activeHarness,
      harnessLocked,
      boardSyncSeq,
      focusSeq,
      refreshSessions,
      selectSession,
      newSession,
      changeModel,
      changeHarness,
      renameSession,
      deleteSession,
      branchFrom,
      send,
      stop,
    ],
  );

  const draftValue = useMemo<DraftValue>(
    () => ({ draft, setDraft }),
    [draft, setDraft],
  );

  return (
    <SessionsContext.Provider value={value}>
      <SegmentsContext.Provider value={segments}>
        <DraftContext.Provider value={draftValue}>
          {children}
        </DraftContext.Provider>
      </SegmentsContext.Provider>
    </SessionsContext.Provider>
  );
}

export function useSessions(): SessionsValue {
  const value = useContext(SessionsContext);
  if (!value) throw new Error("useSessions must be used inside SessionsProvider");
  return value;
}

export function useSegments(): Segment[] {
  const value = useContext(SegmentsContext);
  if (!value) throw new Error("useSegments must be used inside SessionsProvider");
  return value;
}

export function useDraft(): DraftValue {
  const value = useContext(DraftContext);
  if (!value) throw new Error("useDraft must be used inside SessionsProvider");
  return value;
}
