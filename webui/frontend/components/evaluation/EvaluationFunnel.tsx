"use client";

import { formatCount, formatRatio } from "@/lib/evaluation";

export interface FunnelStep {
  label: string;
  value: number;
}

interface EvaluationFunnelProps {
  steps: FunnelStep[];
}

/** Task funnel: created → executed → run completed → review → done. */
export default function EvaluationFunnel({ steps }: EvaluationFunnelProps) {
  const top = Math.max(...steps.map((step) => step.value), 0);
  return (
    <div className="eval-funnel">
      {steps.map((step, index) => {
        const previous = index > 0 ? steps[index - 1].value : null;
        const width = top > 0 ? Math.max((step.value / top) * 100, 1) : 0;
        return (
          <div key={step.label} className="eval-funnel-row">
            <span className="eval-bar-label">{step.label}</span>
            <span className="eval-funnel-track">
              <span
                className="eval-funnel-fill"
                style={{ width: `${width}%` }}
              />
            </span>
            <span className="eval-bar-value">{formatCount(step.value)}</span>
            <span className="eval-funnel-rate">
              {previous === null || previous === 0
                ? ""
                : formatRatio(step.value / previous)}
            </span>
          </div>
        );
      })}
    </div>
  );
}
