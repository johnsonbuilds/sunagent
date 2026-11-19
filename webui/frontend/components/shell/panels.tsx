"use client";

import type { PanelId } from "@/lib/routes";

import BoardPanel from "@/components/BoardPanel";
import SessionsPanel from "@/components/SessionsPanel";

export default function SidebarPanel({
  id,
}: {
  id: NonNullable<PanelId>;
}) {
  if (id === "sessions") return <SessionsPanel />;
  return <BoardPanel />;
}
