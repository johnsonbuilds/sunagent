"use client";

export interface StackSegment {
  label: string;
  value: number;
  /** CSS tone: ``prompt`` / ``completion`` / ``cached`` / ``llm`` / ... */
  tone: string;
}

export interface StackRow {
  label: string;
  segments: StackSegment[];
}

interface StackedBarsProps {
  rows: StackRow[];
  format: (value: number) => string;
  emptyLabel: string;
}

/** Composition bars with a legend: token mix and latency split. */
export default function StackedBars({
  rows,
  format,
  emptyLabel,
}: StackedBarsProps) {
  const legend: StackSegment[] = [];
  for (const row of rows) {
    for (const segment of row.segments) {
      if (!legend.some((item) => item.label === segment.label)) {
        legend.push(segment);
      }
    }
  }
  const total = rows.reduce(
    (sum, row) => sum + row.segments.reduce((acc, s) => acc + s.value, 0),
    0,
  );
  if (!rows.length || total <= 0) {
    return <div className="eval-chart-empty">{emptyLabel}</div>;
  }

  return (
    <div className="eval-stack">
      {rows.map((row) => {
        const rowTotal = row.segments.reduce((acc, s) => acc + s.value, 0);
        const base = Math.max(rowTotal, 1);
        return (
          <div key={row.label} className="eval-stack-row">
            <span className="eval-bar-label" title={row.label}>
              {row.label}
            </span>
            <span className="eval-stack-track">
              {row.segments.map((segment) => (
                <span
                  key={segment.label}
                  className={`eval-seg eval-seg-${segment.tone}`}
                  style={{ width: `${(segment.value / base) * 100}%` }}
                  title={`${segment.label}: ${format(segment.value)}`}
                />
              ))}
            </span>
            <span className="eval-bar-value">{format(rowTotal)}</span>
          </div>
        );
      })}
      <div className="eval-stack-legend">
        {legend.map((segment) => (
          <span key={segment.label} className="eval-legend-item">
            <span className={`eval-seg eval-seg-${segment.tone}`} />
            {segment.label}
          </span>
        ))}
      </div>
    </div>
  );
}
