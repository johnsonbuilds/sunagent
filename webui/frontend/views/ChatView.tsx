"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import Composer from "@/components/Composer";
import Transcript from "@/components/Transcript";
import { useSegments, useSessions } from "@/lib/stores/sessions";

/** Distance from the bottom that still counts as "reading the latest". */
const PINNED_THRESHOLD = 120;
/** Window in which scroll events are our own smooth-scroll animation. */
const AUTOSCROLL_GRACE_MS = 600;

export default function ChatView() {
  const { activeId, send } = useSessions();
  const blocks = useSegments();

  const messagesRef = useRef<HTMLDivElement>(null);
  const pinnedRef = useRef(true);
  const scrollScheduledRef = useRef(false);
  const autoScrollingRef = useRef(false);
  const autoScrollTimerRef = useRef<number | null>(null);
  const [showScroll, setShowScroll] = useState(false);

  // `scroll-behavior: smooth` makes our own jump-to-bottom emit a burst of
  // scroll events; without this guard the arrow would flicker on every
  // appended block.
  const markAutoScroll = useCallback(() => {
    autoScrollingRef.current = true;
    if (autoScrollTimerRef.current !== null) {
      window.clearTimeout(autoScrollTimerRef.current);
    }
    autoScrollTimerRef.current = window.setTimeout(() => {
      autoScrollingRef.current = false;
      autoScrollTimerRef.current = null;
    }, AUTOSCROLL_GRACE_MS);
  }, []);

  const scheduleScroll = useCallback(() => {
    if (scrollScheduledRef.current) return;
    scrollScheduledRef.current = true;
    window.requestAnimationFrame(() => {
      scrollScheduledRef.current = false;
      if (!pinnedRef.current) return;
      const element = messagesRef.current;
      if (!element) return;
      markAutoScroll();
      element.scrollTop = element.scrollHeight;
    });
  }, [markAutoScroll]);

  const scrollToBottom = useCallback(() => {
    pinnedRef.current = true;
    setShowScroll(false);
    const element = messagesRef.current;
    if (!element) return;
    markAutoScroll();
    element.scrollTop = element.scrollHeight;
  }, [markAutoScroll]);

  const handleScroll = useCallback(() => {
    const element = messagesRef.current;
    if (!element) return;
    const distance =
      element.scrollHeight - element.scrollTop - element.clientHeight;
    const pinned = distance < PINNED_THRESHOLD;
    pinnedRef.current = pinned;
    if (autoScrollingRef.current) return;
    setShowScroll(!pinned);
  }, []);

  useEffect(() => {
    pinnedRef.current = true;
    setShowScroll(false);
    scheduleScroll();
  }, [activeId, scheduleScroll]);

  useEffect(() => {
    scheduleScroll();
  }, [blocks, scheduleScroll]);

  useEffect(() => {
    return () => {
      if (autoScrollTimerRef.current !== null) {
        window.clearTimeout(autoScrollTimerRef.current);
      }
    };
  }, []);

  return (
    <>
      <Transcript
        blocks={blocks}
        messagesRef={messagesRef}
        onSuggestion={(text) => void send(text)}
        onScroll={handleScroll}
      />

      <Composer
        showScrollToBottom={showScroll}
        onScrollToBottom={scrollToBottom}
      />
    </>
  );
}
