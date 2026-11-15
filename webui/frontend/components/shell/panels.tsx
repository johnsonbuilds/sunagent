"use client";

import type { PanelId } from "@/lib/routes";

import BoardPanel from "@/components/BoardPanel";
import SessionsPanel from "@/components/SessionsPanel";

/** Which sidebar body belongs to the current destination. */
export default function SidebarPanel({
  id,
}: {
  id: NonNullable<PanelId>;
}) {
  if (id === "sessions") return <SessionsPanel />;
  return <BoardPanel />;
}
