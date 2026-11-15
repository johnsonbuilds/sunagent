import type { MessageKey } from "@/lib/i18n";

/** A short, transient run state shown in the title bar. */
export interface StatusState {
  label: MessageKey;
  kind?: "error";
}
