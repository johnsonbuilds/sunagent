import type { EvalTab, Ratio } from "@/lib/types";

/** Every formatter renders "no sample" as an em dash, never as a zero. */
export const NO_VALUE = "—";

export function formatRatio(value: Ratio | undefined, digits = 0): string {
  if (value === null || value === undefined) return NO_VALUE;
  return `${(value * 100).toFixed(digits)}%`;
}

export function formatCount(value: number | null | undefined): string {
  if (value === null || value === undefined) return NO_VALUE;
  return value.toLocaleString();
}

export function formatTokens(value: number | null | undefined): string {
  if (value === null || value === undefined) return NO_VALUE;
  if (value >= 1_000_000) return `${(value / 1_000_000).toFixed(1)}M`;
  if (value >= 10_000) return `${(value / 1_000).toFixed(1)}k`;
  return Math.round(value).toString();
}

export function formatMs(value: number | null | undefined): string {
  if (value === null || value === undefined) return NO_VALUE;
  if (value >= 600_000) return `${(value / 60_000).toFixed(1)}m`;
  if (value >= 1_000) return `${(value / 1_000).toFixed(1)}s`;
  return `${Math.round(value)}ms`;
}

export function formatBytes(value: number | null | undefined): string {
  if (value === null || value === undefined) return NO_VALUE;
  if (value >= 1_073_741_824) return `${(value / 1_073_741_824).toFixed(1)}GB`;
  if (value >= 1_048_576) return `${(value / 1_048_576).toFixed(1)}MB`;
  if (value >= 1_024) return `${(value / 1_024).toFixed(1)}kB`;
  return `${Math.round(value)}B`;
}

/** ``2026-11-20`` → ``11-20`` (the chart axis has no room for the year). */
export function shortDay(day: string): string {
  return day.length > 5 ? day.slice(5) : day;
}

export function formatDayTime(epoch: number | null | undefined): string {
  if (!epoch) return NO_VALUE;
  const date = new Date(epoch * 1000);
  const pad = (value: number) => String(value).padStart(2, "0");
  return `${date.getFullYear()}-${pad(date.getMonth() + 1)}-${pad(date.getDate())} ` +
    `${pad(date.getHours())}:${pad(date.getMinutes())}`;
}

export const EVAL_TABS: EvalTab[] = [
  "overview",
  "validity",
  "process",
  "efficiency",
  "reliability",
  "tasks",
  "benchmarks",
  "diagnosis",
];

export const EVAL_RANGES = ["24h", "7d", "30d", "90d", "all"] as const;

export const EVAL_ORIGINS = ["webui", "cli", "harbor"] as const;

const CHART_MAX_POINTS = 60;

/** Keep the newest ``CHART_MAX_POINTS`` samples so the SVG stays readable. */
export function trimSeries<T>(points: T[]): T[] {
  return points.length > CHART_MAX_POINTS
    ? points.slice(points.length - CHART_MAX_POINTS)
    : points;
}
