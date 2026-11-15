"use client";

import { useCallback, useEffect, useRef } from "react";

import Composer from "@/components/Composer";
import Transcript from "@/components/Transcript";
import { useSegments, useSessions } from "@/lib/stores/sessions";

export default function ChatView() {
  const { activeId, send } = useSessions();
  const blocks = useSegments();

  const messagesRef = useRef<HTMLDivElement>(null);
  const pinnedRef = useRef(true);
  const scrollScheduledRef = useRef(false);

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

  useEffect(() => {
    pinnedRef.current = true;
    scheduleScroll();
  }, [activeId, scheduleScroll]);

  useEffect(() => {
    scheduleScroll();
  }, [blocks, scheduleScroll]);

  return (
    <>
      <Transcript
        blocks={blocks}
        messagesRef={messagesRef}
        onSuggestion={(text) => void send(text)}
        onScroll={() => {
          const element = messagesRef.current;
          if (!element) return;
          const distance =
            element.scrollHeight - element.scrollTop - element.clientHeight;
          pinnedRef.current = distance < 120;
        }}
      />

      <Composer />
    </>
  );
}
