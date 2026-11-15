"use client";

import Link from "next/link";

import { EmptyMark } from "@/components/Icons";
import { useI18n } from "@/lib/i18n";

/**
 * The static export's 404 page, and the view Next renders for a dynamic
 * path outside `generateStaticParams` in development.
 */
export default function NotFound() {
  const { t } = useI18n();

  return (
    <div className="not-found">
      <div className="not-found-card">
        <EmptyMark />
        <span className="not-found-code">404</span>
        <h1 className="not-found-title">{t("notFoundTitle")}</h1>
        <p className="not-found-desc">{t("notFoundDescription")}</p>
        <Link className="btn primary" href="/">
          {t("notFoundBack")}
        </Link>
      </div>
    </div>
  );
}
