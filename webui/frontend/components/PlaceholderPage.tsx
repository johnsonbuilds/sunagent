"use client";

import type { ReactElement } from "react";

import { useI18n, type MessageKey } from "@/lib/i18n";

interface PlaceholderPageProps {
  titleKey: MessageKey;
  descriptionKey: MessageKey;
  Icon: () => ReactElement;
}

/** Shared empty state for destinations whose data layer is not wired up. */
export default function PlaceholderPage({
  titleKey,
  descriptionKey,
  Icon,
}: PlaceholderPageProps) {
  const { t } = useI18n();

  return (
    <div className="placeholder-page">
      <div className="placeholder-card">
        <span className="placeholder-icon">
          <Icon />
        </span>
        <h1 className="placeholder-title">{t(titleKey)}</h1>
        <p className="placeholder-desc">{t(descriptionKey)}</p>
      </div>
    </div>
  );
}
