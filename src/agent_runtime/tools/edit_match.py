"""edit_match.py — shared SEARCH/old_text matching semantics for edit_file / apply_patch.

Match ladder; each level must resolve to exactly ONE location before it applies:

  L0  exact bytes               old_text is a literal substring of content
  L1  lineno-prefix-stripped    old_text was copied from cat -n / grep -n output
  L2  indent-insensitive        per-line lstrip comparison; CRLF == LF

Rules that make it safe:
  * every level enforces uniqueness (0 or >1 hits -> error, never a guess)
  * non-exact levels are LOCATORS: they return the span to replace and the
    mode used, so the caller reports ``match_mode`` back to the model
  * L2 shifts new_text by the indent delta (file level - old_text level),
    fixing the "off by one nesting level" class
  * when the whole ladder fails, MatchError carries a rendered diagnosis
    (visible tabs, trailing whitespace, best candidate window, file indent
    census, CRLF detection) so the model self-corrects in ONE turn
"""

from __future__ import annotations

import re
from difflib import SequenceMatcher

_LINE_NO = re.compile(r"^\s*\d+[:\t|]")
_LINE_NO_SP = re.compile(r"^\s*\d+[:\t|] ")
_WS = " \t"


class MatchError(ValueError):
    """Ladder failed; str(exc) is model-facing diagnosis text."""


def _indent(line: str) -> str:
    stripped = line.lstrip(_WS)
    return line[: len(line) - len(stripped)]


def _line_starts(content: str) -> list[int]:
    starts, pos = [], 0
    for line in content.splitlines():
        starts.append(pos)
        pos += len(line) + _eol_len(content, pos + len(line))
    return starts


def _eol_len(content: str, pos: int) -> int:
    # splitlines() strips \r\n, so the length consumed per line is the
    # line itself plus its terminator — \r\n counts as 2, not 0.
    if pos < len(content) and content[pos] == "\r":
        return 2 if content.startswith("\r\n", pos) else 1
    if pos < len(content) and content[pos] == "\n":
        return 1
    return 0


def _dominant_eol(content: str) -> str:
    return "\r\n" if "\r\n" in content else "\n"


def _visual(line: str) -> str:
    shown = line.replace("\t", "»")
    stripped = shown.rstrip()
    if stripped != shown:
        shown = stripped + "  ⟵ trailing whitespace"
    return shown


def apply_edit(content: str, old: str, new: str) -> tuple[str, str]:
    """Replace the unique region referenced by ``old`` with ``new``.

    Returns ``(updated_content, match_mode)`` where mode is one of
    ``exact`` / ``lineno-stripped`` / ``indent-insensitive`` /
    ``indent-shifted``.  Raises :class:`MatchError` with a diagnosis when
    no level matches or the reference is ambiguous.
    """
    if not old:
        raise MatchError("old_text must not be empty")

    # -- L0: exact bytes (current semantics, unchanged) --------------------
    hits = _exact_hits(content, old)
    if len(hits) == 1:
        start = hits[0]
        return content[:start] + new + content[start + len(old):], "exact"
    if len(hits) > 1:
        raise MatchError(_ambiguous_msg(content, old, hits, level="exact"))

    # -- L1: line-number prefixes copied from cat -n / grep -n -------------
    old_lines = old.splitlines()
    if len(old_lines) >= 2:
        for stripped in _lineno_variants(old_lines):
            hits = _exact_hits(content, stripped)
            if len(hits) == 1:
                start = hits[0]
                return (content[:start] + new
                        + content[start + len(stripped):], "lineno-stripped")
            if len(hits) > 1:
                raise MatchError(_ambiguous_msg(content, old, hits,
                                                level="lineno-stripped"))

    # -- L2: indent-insensitive whole-line window --------------------------
    start, end, window_lines = _unique_window(content, old)
    replacement, mode = _make_replacement(new, old, window_lines,
                                          _dominant_eol(content))
    if not replacement and end < len(content):
        # whole-line deletion: consume the window's trailing EOL unit too
        if content.startswith("\r\n", end):
            end += 2
        elif content[end] in "\r\n":
            end += 1
    return content[:start] + replacement + content[end:], mode


def _lineno_variants(old_lines: list[str]) -> list[str]:
    """Candidate de-numbered forms of old_text.

    ``12:LINE`` (grep/rg) and ``    12\tLINE`` (cat -n) carry no separator
    whitespace, but awk-style ``12: LINE`` adds one — and swallowing that
    space blindly would also eat the first space of ``12:    LINE``.  So we
    try both strip widths and let uniqueness decide.
    """
    if not any(_LINE_NO.match(ln) for ln in old_lines):
        return []
    no_ws = "\n".join(_LINE_NO.sub("", ln) for ln in old_lines)
    one_ws = "\n".join(_LINE_NO_SP.sub("", ln) for ln in old_lines)
    out = []
    for v in (no_ws, one_ws):
        if v not in out:
            out.append(v)
    return out


def _exact_hits(content: str, needle: str) -> list[int]:
    hits, pos = [], content.find(needle)
    while pos != -1:
        hits.append(pos)
        pos = content.find(needle, pos + 1)
    return hits


def _unique_window(content: str, old: str) -> tuple[int, int, list[str]]:
    """Whole-line window whose lstrip'd lines equal old's lstrip'd lines."""
    c_lines = content.splitlines()
    o_lines = old.splitlines()
    m = len(o_lines)
    if m == 0:
        raise MatchError("old_text is empty")
    if len(c_lines) < m:
        raise MatchError(_not_found_msg(content, old))
    starts = _line_starts(content)
    hits = [i for i in range(len(c_lines) - m + 1)
            if all(a.lstrip(_WS) == b.lstrip(_WS)
                   for a, b in zip(c_lines[i:i + m], o_lines))]
    if len(hits) == 1:
        i = hits[0]
        start = starts[i]
        end = starts[i + m - 1] + len(c_lines[i + m - 1])
        return start, end, c_lines[i:i + m]
    if len(hits) > 1:
        ranges = ", ".join(f"lines {i + 1}-{i + m}" for i in hits[:8])
        raise MatchError(
            f"old_text is ambiguous: matches {len(hits)} locations "
            f"({ranges}); include more surrounding lines to disambiguate")
    raise MatchError(_not_found_msg(content, old))


def _make_replacement(new: str, old: str, window_lines: list[str],
                      eol: str) -> tuple[str, str]:
    """L2 apply: shift new_text by the block's rigid indent delta, file's EOL.

    The shift applies only when every non-blank line pair shows the same
    indent delta (a rigid whole-block offset — the common "miscounted
    nesting level" failure).  Ragged per-line mismatches are not guessed
    at: new_text lands as authored and the mode says so.
    """
    deltas: list[int] = []
    same_char = True
    for o, w in zip(old.splitlines(), window_lines):
        if not o.strip():
            continue  # blank lines carry no indent information
        fi, oi = _indent(w), _indent(o)
        if fi and oi and fi[0] != oi[0]:
            same_char = False  # tab vs space: never translate
            break
        deltas.append(len(fi) - len(oi))
    # shifting new_text whose indent character differs from old_text's would
    # splice mixed indentation into the file — leave it as authored
    new_chars = {_indent(ln)[0] for ln in new.splitlines()
                 if ln.strip() and _indent(ln)}
    old_chars = {ln[0] for ln in
                 (_indent(o) for o in old.splitlines() if o.strip() and _indent(o))}
    if new_chars and old_chars and new_chars != old_chars:
        same_char = False
    delta = deltas[0] if same_char and deltas and len(set(deltas)) == 1 else 0
    lines = new.splitlines()
    if delta:
        unit = oi[0]
        shifted = []
        for ln in lines:
            if not ln.strip():
                shifted.append("")
            elif delta > 0:
                shifted.append(unit * delta + ln)
            else:
                cut = min(-delta, len(_indent(ln)))
                shifted.append(ln[cut:])
        lines = shifted
    mode = "indent-shifted" if delta else "indent-insensitive"
    return eol.join(lines), mode


def _ambiguous_msg(content: str, old: str, hits: list[int],
                   level: str) -> str:
    lines = content.splitlines()
    starts = _line_starts(content)
    to_line = {s: i + 1 for i, s in enumerate(starts)}
    where = [f"line {to_line.get(h, '?')}" for h in hits[:8]]
    return (f"old_text appears {len(hits)} times in the file ({level}); "
            f"matches at {', '.join(where)}. "
            "Include more surrounding lines to make it unique.")


def _not_found_msg(content: str, old: str) -> str:
    """Best-effort diagnosis: WHY did it not match, in model-readable form."""
    out = ["old_text not found at any match level. Diagnosis:"]
    o_lines, c_lines = old.splitlines(), content.splitlines()

    if any(_LINE_NO.match(ln) for ln in o_lines):
        out.append("  - old_text lines carry line-number prefixes "
                   "(e.g. '123:' from cat -n / grep -n); the file does not "
                   "contain them.")

    tab_lines = sum(1 for ln in c_lines if ln.startswith("\t"))
    sp_lines = sum(1 for ln in c_lines
                   if ln[: len(ln) - len(ln.lstrip(" "))].strip() == "")
    crlf = "\r\n" in content
    trailing = sum(1 for ln in c_lines if ln != ln.rstrip())
    out.append(f"  - file: {len(c_lines)} lines, tab-indented: {tab_lines}, "
               f"space-indented: {sp_lines}, trailing-ws lines: {trailing}"
               + (", CRLF line endings" if crlf else ""))

    if crlf and "\n" in old and "\r" not in old and len(o_lines) > 1:
        out.append("  - CRLF file + LF old_text: multi-line old_text can never "
                   "exact-match; the line-window level handles it, but its "
                   "lstripped lines must equal yours.")

    best, score = _best_window(c_lines, o_lines)
    if best is not None and score > 0.3:
        i, m = best, len(o_lines)
        lo, hi = max(0, i - 2), min(len(c_lines), i + m + 2)
        out.append(f"  - closest region (similarity {score:.0%}), "
                   f"lines {i + 1}-{i + m}:")
        for j in range(lo, hi):
            marker = ">" if i <= j < i + m else " "
            out.append(f"    {marker}{j + 1:5d}| {_visual(c_lines[j])}")
    out.append("Re-copy old_text from this region, or address the "
               "diagnosis above and retry.")
    return "\n".join(out)


def _best_window(c_lines: list[str], o_lines: list[str]) \
        -> tuple[int | None, float]:
    """Best window by mean per-line character similarity (lstripped)."""
    m = len(o_lines)
    if m == 0 or len(c_lines) < m:
        return None, 0.0
    key_o = [ln.lstrip(_WS) for ln in o_lines]
    best, best_score = None, 0.0
    for i in range(len(c_lines) - m + 1):
        window = [ln.lstrip(_WS) for ln in c_lines[i:i + m]]
        score = sum(SequenceMatcher(None, a, b).ratio()
                    for a, b in zip(window, key_o)) / m
        if score > best_score:
            best, best_score = i, score
    return best, best_score


__all__ = ["MatchError", "apply_edit"]
