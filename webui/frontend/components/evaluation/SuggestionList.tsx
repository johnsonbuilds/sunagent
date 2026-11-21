"use client";

import Markdown from "@/components/Markdown";
import { useI18n } from "@/lib/i18n";
import type { EvalDiagnosisSuggestion } from "@/lib/types";

interface SuggestionListProps {
  suggestions: EvalDiagnosisSuggestion[];
}

/** How many suggestions the report contract asks for (docs/evaluation.md §7). */
const EXPECTED_SUGGESTIONS = 5;

/**
 * The ``Top 5`` list, extracted by the backend so the rendering never
 * guesses: fewer than five items is reported, not padded (docs §7).
 */
export default function SuggestionList({ suggestions }: SuggestionListProps) {
  const { t } = useI18n();
  if (!suggestions.length) {
    return <div className="eval-chart-empty">{t("evalNoSuggestions")}</div>;
  }
  return (
    <>
      <ol className="eval-suggestions">
        {suggestions.map((item) => (
          <li key={item.index} className="eval-suggestion">
            <Markdown text={item.text} className="eval-suggestion-body" />
          </li>
        ))}
      </ol>
      {suggestions.length < EXPECTED_SUGGESTIONS ? (
        <p className="eval-report-hint">
          {t("evalSuggestionsFew", { count: suggestions.length })}
        </p>
      ) : null}
    </>
  );
}
