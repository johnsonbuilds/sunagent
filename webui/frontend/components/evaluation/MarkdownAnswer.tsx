"use client";

import Markdown from "@/components/Markdown";
import { useI18n } from "@/lib/i18n";
import type { EvalDiagnosisReport } from "@/lib/types";
import SuggestionList from "@/components/evaluation/SuggestionList";

/** The four headings the report contract fixes (docs/evaluation.md §7). */
export const REPORT_HEADINGS = ["结论", "根因", "证据", "Top 5 改进建议"] as const;

const HEADING_RE = /^#{1,3}\s*(结论|根因|证据|top\s*5\s*改进建议)\s*$/gim;

export interface ReportSection {
  heading: string;
  body: string;
}

function normalize(raw: string): string {
  const text = raw.replace(/\s+/g, " ").trim().toLowerCase();
  if (text.startsWith("top")) return REPORT_HEADINGS[3];
  const found = REPORT_HEADINGS.find((heading) => heading.toLowerCase() === text);
  return found ?? raw.trim();
}

/**
 * Split a report into its fixed sections; unknown headings stay in the body.
 *
 * Text before the first fixed heading (a one-line preamble) is kept as a
 * section with an empty heading instead of being silently dropped.
 */
export function splitReport(markdown: string): ReportSection[] {
  const text = markdown ?? "";
  const matches = [...text.matchAll(HEADING_RE)];
  const sections: ReportSection[] = [];
  const preamble = (matches[0]?.index ?? text.length) > 0
    ? text.slice(0, matches[0]?.index ?? text.length).trim()
    : "";
  if (preamble) sections.push({ heading: "", body: preamble });
  for (const [index, match] of matches.entries()) {
    const start = (match.index ?? 0) + match[0].length;
    const end = index + 1 < matches.length
      ? matches[index + 1].index ?? text.length
      : text.length;
    sections.push({ heading: normalize(match[1]), body: text.slice(start, end).trim() });
  }
  return sections;
}

interface MarkdownAnswerProps {
  report: EvalDiagnosisReport;
}

/**
 * The diagnosis report: four sections plus format and citation annotations.
 *
 * Both annotations are best-effort (docs/evaluation.md §7): a missing heading
 * or an unverified citation is shown next to the text, never used to hide it.
 * The suggestions section is rendered from the extracted items, so callers
 * must not render ``SuggestionList`` again below it.
 */
export default function MarkdownAnswer({ report }: MarkdownAnswerProps) {
  const { t } = useI18n();
  const markdown = report.markdown ?? "";
  if (!report.available || !markdown.trim()) {
    return (
      <div className="eval-chart-empty">{t("evalReportPending")}</div>
    );
  }
  const sections = splitReport(markdown);
  const verification = report.verification;
  const missing = verification?.missing_sections ?? [];
  const suggestions = report.suggestions ?? [];
  const unverified = verification?.unverified ?? [];
  const incomplete = missing.length > 0
    || (verification !== undefined && verification !== null && !verification.order_ok);
  return (
    <div className="eval-report">
      {incomplete ? (
        <p className="eval-report-warning">
          {t("evalReportIncomplete")}
          {missing.length > 0
            ? `: ${t("evalReportMissing")} ${missing.join(" / ")}`
            : ""}
        </p>
      ) : null}
      {unverified.length > 0 ? (
        <p className="eval-report-warning">
          {t("evalUnverified")}:{" "}
          {unverified.map((value) => (
            <code key={value} className="eval-citation-bad">
              {value}
            </code>
          ))}
        </p>
      ) : null}
      {sections.length === 0 ? (
        <Markdown text={markdown} className="eval-report-body" />
      ) : (
        sections.map((section, index) => (
          <section key={`${section.heading}-${index}`} className="eval-report-section">
            {section.heading ? (
              <h4 className="eval-subhead">{section.heading}</h4>
            ) : null}
            {section.heading === REPORT_HEADINGS[3] && suggestions.length ? (
              <SuggestionList suggestions={suggestions} />
            ) : (
              <Markdown text={section.body} className="eval-report-body" />
            )}
          </section>
        ))
      )}
    </div>
  );
}
