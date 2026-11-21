"use client";

import type { ReactNode } from "react";

interface SectionProps {
  title: string;
  hint?: string;
  actions?: ReactNode;
  className?: string;
  children: ReactNode;
}

export default function Section({
  title,
  hint,
  actions,
  className,
  children,
}: SectionProps) {
  return (
    <section className={`eval-section${className ? ` ${className}` : ""}`}>
      <header className="eval-section-head">
        <h2 className="eval-section-title">{title}</h2>
        {hint ? <span className="eval-section-hint">{hint}</span> : null}
        {actions ? <div className="eval-section-actions">{actions}</div> : null}
      </header>
      {children}
    </section>
  );
}
