"use client";

import { formatRatio } from "@/lib/evaluation";

export type KpiTone = "default" | "good" | "warn" | "bad";

export interface KpiCard {
  label: string;
  value: string;
  hint?: string;
  tone?: KpiTone;
  /** Numeric value used for the threshold colour; ``null`` is neutral. */
  ratio?: number | null;
  /** Lower is better (rework): flips the threshold direction. */
  invert?: boolean;
}

interface EvaluationKpisProps {
  cards: KpiCard[];
}

function toneClass(card: KpiCard): string {
  if (card.tone) return card.tone === "default" ? "" : ` ${card.tone}`;
  const ratio = card.ratio;
  if (ratio === null || ratio === undefined) return "";
  if (card.invert) {
    if (ratio <= 0.1) return " good";
    if (ratio <= 0.3) return " warn";
    return " bad";
  }
  if (ratio >= 0.9) return " good";
  if (ratio >= 0.6) return " warn";
  return " bad";
}

export default function EvaluationKpis({ cards }: EvaluationKpisProps) {
  return (
    <div className="eval-kpis">
      {cards.map((card) => (
        <div key={card.label} className={`eval-kpi${toneClass(card)}`}>
          <span className="eval-kpi-label">{card.label}</span>
          <span className="eval-kpi-value">{card.value}</span>
          {card.hint ? (
            <span className="eval-kpi-hint">{card.hint}</span>
          ) : null}
        </div>
      ))}
    </div>
  );
}

export interface RatioCardOptions {
  hint?: string;
  invert?: boolean;
}

/** Shared shape for the ratio cards so pages never re-implement them. */
export function ratioCard(
  label: string,
  value: number | null | undefined,
  options: RatioCardOptions = {},
): KpiCard {
  return {
    label,
    value: formatRatio(value),
    hint: options.hint,
    ratio: value ?? null,
    invert: options.invert,
  };
}
