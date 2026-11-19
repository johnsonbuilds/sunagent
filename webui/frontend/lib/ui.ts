import type { MessageKey } from "@/lib/i18n";

export interface StatusState {
  label: MessageKey;
  kind?: "error";
}
