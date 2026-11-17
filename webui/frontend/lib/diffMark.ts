/* Merge intra-line word ranges into highlight.js HTML.
 *
 * The highlighter returns HTML with its own `<span>`s; the ranges are
 * character offsets into the plain line.  This walker inserts a marker
 * span at every range boundary and closes/reopens it at tag boundaries,
 * so the output is always correctly nested — a range may start inside
 * one syntax token and end inside the next.
 */

import { escapeHtml } from "@/lib/markdown";
import type { DiffRange } from "@/lib/wordDiff";

const MARK_CLASS = "diff-word";
const ENTITY = /^&(?:#\d+|#x[0-9a-fA-F]+|[a-zA-Z]+);/;

/**
 * Map decoded character offsets of one text node to raw offsets, so a
 * range boundary that falls on `&amp;` never splits the entity.
 * Returns null when the text has no entities and raw offsets are equal.
 */
function rawOffsets(raw: string): number[] | null {
  if (!raw.includes("&")) return null;
  const offsets: number[] = [0];
  let index = 0;
  while (index < raw.length) {
    if (raw[index] === "&") {
      const match = ENTITY.exec(raw.slice(index, index + 12));
      if (match) {
        index += match[0].length;
        offsets.push(index);
        continue;
      }
    }
    index += 1;
    offsets.push(index);
  }
  return offsets;
}

function slice(
  raw: string,
  offsets: number[] | null,
  from: number,
  to: number,
): string {
  return offsets ? raw.slice(offsets[from], offsets[to]) : raw.slice(from, to);
}

/** Wrap the given character ranges in `diff-word` spans. */
export function markRanges(html: string, ranges: DiffRange[]): string {
  if (!html || ranges.length === 0) return html;

  const out: string[] = [];
  let cursor = 0;
  let offset = 0;
  let wrapped = false;

  // Ranges are sorted and merged, and offsets only move forward, so one
  // pointer is enough to answer "is this offset inside a range?".
  const isActive = (position: number): boolean => {
    while (cursor < ranges.length && ranges[cursor].end <= position) {
      cursor += 1;
    }
    return cursor < ranges.length && ranges[cursor].start <= position;
  };

  const pieces = html.match(/<[^>]*>|[^<]+/g) ?? [];
  for (const piece of pieces) {
    if (piece.startsWith("<")) {
      // A range that spans this tag is closed before it and reopened by
      // the next text node, keeping every element properly nested.
      if (wrapped && piece.startsWith("</")) {
        out.push("</span>");
        wrapped = false;
      }
      out.push(piece);
      continue;
    }

    const offsets = rawOffsets(piece);
    const length = offsets ? offsets.length - 1 : piece.length;
    if (length <= 0) continue;

    const cuts: number[] = [];
    for (const range of ranges) {
      if (range.start > offset && range.start < offset + length) {
        cuts.push(range.start - offset);
      }
      if (range.end > offset && range.end < offset + length) {
        cuts.push(range.end - offset);
      }
    }
    cuts.sort((left, right) => left - right);

    let from = 0;
    const emit = (to: number) => {
      if (to <= from) return;
      const active = isActive(offset + from);
      if (active !== wrapped) {
        out.push(active ? `<span class="${MARK_CLASS}">` : "</span>");
        wrapped = active;
      }
      out.push(slice(piece, offsets, from, to));
      from = to;
    };
    for (const cut of cuts) emit(cut);
    emit(length);

    offset += length;
  }

  if (wrapped) out.push("</span>");
  return out.join("");
}

/** Same marker for a plain, unhighlighted line. */
export function markText(text: string, ranges: DiffRange[]): string {
  return markRanges(escapeHtml(text), ranges);
}
