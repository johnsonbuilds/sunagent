"use client";

import { NO_VALUE } from "@/lib/evaluation";

export interface BarRow {
  label: string;
  /** ``null`` means "no sample": the bar collapses and the value renders ``—``. */
  value: number | null;
  hint?: string;
}

interface BarListProps {
  rows: BarRow[];
  format: (value: number) => string;
  emptyLabel: string;
  /** Cap on rendered rows; the tail is summarised as "+N more". */
  limit?: number;
}

/** Horizontal bars: tool errors by tool, failure reasons, slice rates. */
export default function BarList({
  rows,
  format,
  emptyLabel,
  limit = 12,
}: BarListProps) {
  if (rows.length === 0) {
    return <div className="eval-chart-empty">{emptyLabel}</div>;
  }
  const shown = rows.slice(0, limit);
  const hidden = rows.length - shown.length;
  const max = Math.max(
    ...shown.map((row) => (row.value === null ? 0 : row.value)),
    1,
  );

  return (
    <div className="eval-bars">
      {shown.map((row) => (
        <div key={row.label} className="eval-bar-row">
          <span className="eval-bar-label" title={row.label}>
            {row.label}
          </span>
          <span className="eval-bar-track">
            <span
              className="eval-bar-fill"
              style={{
                width:
                  row.value === null
                    ? "0%"
                    : `${Math.max((row.value / max) * 100, 1.5)}%`,
              }}
            />
          </span>
          <span className="eval-bar-value">
            {row.value === null ? NO_VALUE : format(row.value)}
          </span>
          {row.hint ? <span className="eval-bar-hint">{row.hint}</span> : null}
        </div>
      ))}
      {hidden > 0 ? (
        <div className="eval-bar-more">+{hidden}</div>
      ) : null}
    </div>
  );
}
