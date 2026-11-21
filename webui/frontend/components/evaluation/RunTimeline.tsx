"use client";

import { formatDayTime, formatMs } from "@/lib/evaluation";
import type { EvalTimelineEvent } from "@/lib/types";

interface RunTimelineProps {
  events: EvalTimelineEvent[];
  emptyLabel: string;
}

/** Board lifecycle of one task, straight from ``board_events.jsonl``. */
export default function RunTimeline({ events, emptyLabel }: RunTimelineProps) {
  if (events.length === 0) {
    return <div className="eval-chart-empty">{emptyLabel}</div>;
  }
  return (
    <ol className="eval-timeline">
      {events.map((event, index) => (
        <li key={`${event.type}-${event.at}-${index}`} className="eval-timeline-item">
          <span className={`eval-timeline-dot eval-timeline-${event.type}`} />
          <span className="eval-timeline-type">{event.type}</span>
          <span className="eval-timeline-at">{formatDayTime(event.at)}</span>
          {event.from || event.to ? (
            <span className="eval-timeline-move">
              {event.from ?? "—"} → {event.to ?? "—"}
            </span>
          ) : null}
          {event.queue_wait_ms !== null ? (
            <span className="eval-timeline-wait">
              {formatMs(event.queue_wait_ms)}
            </span>
          ) : null}
          {event.run_id ? (
            <span className="eval-timeline-run">{event.run_id}</span>
          ) : null}
        </li>
      ))}
    </ol>
  );
}
