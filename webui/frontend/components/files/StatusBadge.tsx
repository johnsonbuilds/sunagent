"use client";

import { statusKey, statusLabel } from "@/lib/files";
import { useI18n } from "@/lib/i18n";
import type { ChangeStatus } from "@/lib/types";

export default function StatusBadge({
  status,
  title,
}: {
  status: ChangeStatus;
  title?: string;
}) {
  const { t } = useI18n();
  const label = title ?? t(statusKey(status));
  return (
    <span
      className={`files-badge is-${status}`}
      title={label}
      aria-label={label}
    >
      {statusLabel(status)}
    </span>
  );
}
