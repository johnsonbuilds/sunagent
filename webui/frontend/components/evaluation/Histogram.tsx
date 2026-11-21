"use client";

import { formatCount } from "@/lib/evaluation";
import type { EvalHistogramRow } from "@/lib/types";

interface HistogramProps {
  rows: EvalHistogramRow[];
  emptyLabel: string;
}

/** Fixed-bucket distribution (iterations, run duration). */
export default function Histogram({ rows, emptyLabel }: HistogramProps) {
  const total = rows.reduce((sum, row) => sum + row.count, 0);
  if (!rows.length || total === 0) {
    return <div className="eval-chart-empty">{emptyLabel}</div>;
  }
  const max = Math.max(...rows.map((row) => row.count), 1);

  return (
    <div className="eval-histogram">
      {rows.map((row) => (
        <div key={row.label} className="eval-histogram-col">
          <span className="eval-histogram-count">{formatCount(row.count)}</span>
          <span className="eval-histogram-track">
            <span
              className="eval-histogram-fill"
              style={{ height: `${(row.count / max) * 100}%` }}
              title={`${row.label}: ${formatCount(row.count)}`}
            />
          </span>
          <span className="eval-histogram-label">{row.label}</span>
        </div>
      ))}
    </div>
  );
}
