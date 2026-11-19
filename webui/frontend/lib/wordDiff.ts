export interface DiffRange {
  /** Character offset of the first changed character, inclusive. */
  start: number;
  /** Character offset just past the last changed character. */
  end: number;
}

export interface WordDiffOutcome {
  /** Changed ranges in the old line, or null when marking is not useful. */
  old: DiffRange[] | null;
  /** Changed ranges in the new line, or null when marking is not useful. */
  new: DiffRange[] | null;
  /** Rough work performed, so callers can stop after a total budget. */
  cost: number;
}

interface Token {
  text: string;
  start: number;
  end: number;
}

/** Above this length a line is left to the plain line tint. */
const MAX_LINE_LENGTH = 2000;

/** Largest token matrix we are willing to fill for a single pair. */
const MAX_CELLS = 25_000;

/** Above this share of changed characters the whole line is "changed". */
const MAX_CHANGED_RATIO = 0.6;

/** A pair that produces more than this many ranges is too noisy to mark. */
const MAX_RANGES = 200;

const NOTHING: WordDiffOutcome = { old: null, new: null, cost: 1 };

function tokenize(text: string): Token[] {
  const re = /[A-Za-z0-9_]+|\s+|[^A-Za-z0-9_\s]/g;
  const tokens: Token[] = [];
  let match: RegExpExecArray | null;
  while ((match = re.exec(text)) !== null) {
    tokens.push({
      text: match[0],
      start: match.index,
      end: match.index + match[0].length,
    });
  }
  return tokens;
}

function toRanges(tokens: Token[]): DiffRange[] {
  const ranges: DiffRange[] = [];
  for (const token of tokens) {
    const last = ranges[ranges.length - 1];
    if (last && token.start <= last.end) {
      last.end = Math.max(last.end, token.end);
    } else {
      ranges.push({ start: token.start, end: token.end });
    }
  }
  return ranges;
}

function changedChars(ranges: DiffRange[]): number {
  return ranges.reduce((total, range) => total + range.end - range.start, 0);
}

export function wordDiff(oldText: string, newText: string): WordDiffOutcome {
  if (!oldText || !newText || oldText === newText) return NOTHING;
  if (oldText.length > MAX_LINE_LENGTH || newText.length > MAX_LINE_LENGTH) {
    return NOTHING;
  }

  const oldTokens = tokenize(oldText);
  const newTokens = tokenize(newText);

  let prefix = 0;
  while (
    prefix < oldTokens.length &&
    prefix < newTokens.length &&
    oldTokens[prefix].text === newTokens[prefix].text
  ) {
    prefix += 1;
  }
  let suffix = 0;
  while (
    suffix < oldTokens.length - prefix &&
    suffix < newTokens.length - prefix &&
    oldTokens[oldTokens.length - 1 - suffix].text ===
      newTokens[newTokens.length - 1 - suffix].text
  ) {
    suffix += 1;
  }

  const oldMiddle = oldTokens.slice(prefix, oldTokens.length - suffix);
  const newMiddle = newTokens.slice(prefix, newTokens.length - suffix);
  const cells = oldMiddle.length * newMiddle.length;
  const cost = oldTokens.length + newTokens.length + cells;
  if (oldMiddle.length === 0 && newMiddle.length === 0) return NOTHING;
  if (cells > MAX_CELLS) return { old: null, new: null, cost };

  const oldChanged: Token[] = [];
  const newChanged: Token[] = [];

  if (oldMiddle.length === 0 || newMiddle.length === 0) {
    // Pure insertion or deletion: the whole middle is new.
    oldChanged.push(...oldMiddle);
    newChanged.push(...newMiddle);
  } else {
    const n = oldMiddle.length;
    const m = newMiddle.length;
    const width = m + 1;
    // table[i * width + j] = LCS length of oldMiddle[i:] and newMiddle[j:].
    const table = new Int32Array((n + 1) * width);
    for (let i = n - 1; i >= 0; i -= 1) {
      const row = i * width;
      const next = row + width;
      for (let j = m - 1; j >= 0; j -= 1) {
        if (oldMiddle[i].text === newMiddle[j].text) {
          table[row + j] = table[next + j + 1] + 1;
        } else {
          const down = table[next + j];
          const right = table[row + j + 1];
          table[row + j] = down >= right ? down : right;
        }
      }
    }

    let i = 0;
    let j = 0;
    while (i < n && j < m) {
      if (oldMiddle[i].text === newMiddle[j].text) {
        i += 1;
        j += 1;
        continue;
      }
      const row = i * width;
      const down = table[row + width + j];
      const right = table[row + j + 1];
      if (down >= right) {
        oldChanged.push(oldMiddle[i]);
        i += 1;
      } else {
        newChanged.push(newMiddle[j]);
        j += 1;
      }
    }
    while (i < n) oldChanged.push(oldMiddle[i++]);
    while (j < m) newChanged.push(newMiddle[j++]);
  }

  const oldRanges = toRanges(oldChanged);
  const newRanges = toRanges(newChanged);
  if (oldRanges.length + newRanges.length > MAX_RANGES) {
    return { old: null, new: null, cost };
  }

  const changed = changedChars(oldRanges) + changedChars(newRanges);
  if (changed === 0 || changed > (oldText.length + newText.length) * MAX_CHANGED_RATIO) {
    return { old: null, new: null, cost };
  }

  return { old: oldRanges, new: newRanges, cost };
}
