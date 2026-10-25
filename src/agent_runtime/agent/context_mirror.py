"""Self-managed context mirror: render the request view to an editable file.

The full conversation (``messages``) is never mutated; only the request
projection (``view``) may be replaced by :func:`apply_mirror`.  The format
here is original to SunAgent (``## turn <k> [role]``); it only borrows the
idea of "context as a file" from the CLM literature.
"""

from __future__ import annotations

import copy
import re
from dataclasses import dataclass, field
from typing import Any

HEADER_RE = re.compile(r"^## turn\s+(\d+)\s*\[(.+?)\]\s*$", re.M)
TOOL_SPEC_RE = re.compile(r"^tool(?::(\S+))?(?:\s+id=(\S+))?\s*$", re.IGNORECASE)
KEEP_MARK = "[KEEP-PAIRING]"

MIRROR_FORMAT_VERSION = "LIVE-CTX v1"

#: A sync whose char delta is below this is mirror bookkeeping noise
#: (whitespace, header re-render), not a model edit — no receipt.
TRIVIAL_SYNC_CHARS = 32


def _echo_enabled() -> bool:
    """Whether reasoning_content rides the wire (budget must match)."""
    try:
        from agent_runtime.providers.llm import reasoning_echo_enabled
        return bool(reasoning_echo_enabled())
    except Exception:
        return False


def message_chars(message: dict[str, Any], count_reasoning: bool | None = None) -> int:
    """Char budget for one message, same ruler as ``agent.memory``."""
    if count_reasoning is None:
        count_reasoning = _echo_enabled()
    total = len(message.get("content") or "")
    if count_reasoning:
        total += len(message.get("reasoning_content") or "")
    for call in message.get("tool_calls") or []:
        function = (call or {}).get("function") or {}
        total += len(function.get("arguments") or "")
    return total


def total_chars(messages: list[dict[str, Any]],
                count_reasoning: bool | None = None) -> int:
    if count_reasoning is None:
        count_reasoning = _echo_enabled()
    return sum(message_chars(m, count_reasoning) for m in messages)


def _rendered_text(message: dict[str, Any], include_reasoning: bool) -> str:
    parts: list[str] = []
    if include_reasoning and message.get("reasoning_content"):
        parts.append(str(message["reasoning_content"]))
    content = message.get("content")
    if content:
        parts.append(str(content))
    for call in message.get("tool_calls") or []:
        function = (call or {}).get("function") or {}
        name = function.get("name") or "tool"
        args = function.get("arguments") or ""
        piece = f"{name} {args}".strip()
        if piece:
            parts.append(piece)
    return "\n".join(p for p in parts if p)


def render_mirror(view: list[dict[str, Any]], *, protect: int,
                  limit_chars: int = 0) -> str:
    """Render ``view[protect:]`` as editable markdown."""
    total = len(view)
    safe_protect = max(0, min(int(protect), total))
    include_reasoning = _echo_enabled()
    used = total_chars(view, include_reasoning)
    lines = [
        f"# {MIRROR_FORMAT_VERSION} (editable below; header is metadata)",
        f"# budget: {used}/{limit_chars or 'unset'} chars | "
        f"protect: {safe_protect} | turns: {total - safe_protect}",
        "# RULES: edit turn BODIES only; keep every `## turn <k> [role]` "
        "line byte-identical;",
        "#   empty body drops that turn; bare `tool` turns without "
        f"`{KEEP_MARK}` become plain notes.",
    ]
    for number, index in enumerate(range(safe_protect, total), start=1):
        message = view[index]
        role = message.get("role") or "user"
        if role == "assistant":
            header = f"## turn {number} [assistant]"
        elif role == "tool":
            name = _tool_name_for(view, index)
            call_id = message.get("tool_call_id") or ""
            suffix = f":{name}" if name else ""
            id_part = f" id={call_id}" if call_id else ""
            header = f"## turn {number} [tool{suffix}{id_part}]"
        else:
            header = f"## turn {number} [user]"
        lines.append("")
        lines.append(header)
        lines.append(_rendered_text(message, include_reasoning))
    return "\n".join(lines) + "\n"


def _tool_name_for(view: list[dict[str, Any]], index: int) -> str:
    call_id = view[index].get("tool_call_id")
    if call_id:
        for message in view[:index]:
            for call in message.get("tool_calls") or []:
                if call.get("id") == call_id:
                    return (call.get("function") or {}).get("name") or "tool"
    return "tool"


def _parse_header(spec: str) -> tuple[str, str | None, str | None]:
    """Return (role, tool_name, call_id); unknown roles fold to user."""
    text = spec.strip()
    lowered = text.lower()
    if lowered == "assistant":
        return "assistant", None, None
    if lowered == "user":
        return "user", None, None
    match = TOOL_SPEC_RE.match(text)
    if match:
        return "tool", match.group(1), match.group(2)
    return "user", None, None


def _find_pair(messages: list[dict[str, Any]],
               call_id: str) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Locate the original assistant+tool pair for ``call_id``."""
    for i, message in enumerate(messages):
        if message.get("role") != "assistant":
            continue
        for call in message.get("tool_calls") or []:
            if call.get("id") == call_id:
                for j in range(i + 1, len(messages)):
                    other = messages[j]
                    if other.get("role") == "tool" and other.get("tool_call_id") == call_id:
                        return message, other
                return None
    return None


@dataclass
class ApplyResult:
    status: str  # applied|rejected_noop|rejected_broke_pairing|rejected_grew
    before_chars: int = 0
    after_chars: int = 0
    turns: int = 0
    note: str = ""
    new_view: list[dict[str, Any]] | None = field(default=None)


def _merge_same_role(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    for item in items:
        if (merged and merged[-1].get("role") == item.get("role")
                and item.get("role") in ("user", "assistant")):
            prev = merged[-1].get("content") or ""
            cur = item.get("content") or ""
            merged[-1] = {"role": item["role"],
                          "content": (prev + "\n\n" + cur).strip()}
        else:
            merged.append(item)
    return merged


def _strip_metadata(text: str) -> str:
    """Drop ``#`` metadata lines so the waterline can't fake an edit.

    ``render_mirror`` embeds a live ``used`` char count in the ``# budget:``
    line; the workspace file holds last turn's count, so a raw string
    compare is always "changed".  Only ``## turn`` sections count.
    """
    body = text if isinstance(text, str) else str(text)
    kept = [ln for ln in body.splitlines() if not ln.strip().startswith("#")]
    return "\n".join(kept).strip()


def _is_section_prefix(file_bodies: list[str],
                       live_bodies: list[str]) -> bool:
    """Whether the file's sections are a strict prefix of the live view's."""
    if not file_bodies or len(file_bodies) >= len(live_bodies):
        return False
    return file_bodies == live_bodies[:len(file_bodies)]


def _extract_sections(
        body: str) -> list[tuple[str, str | None, str | None, str]] | None:
    """Split mirror text into ``(role, tool_name, call_id, chunk)`` sections."""
    text = body if isinstance(body, str) else str(body)
    matches = list(HEADER_RE.finditer(text))
    if not matches:
        return None
    sections: list[tuple[str, str | None, str | None, str]] = []
    lead = text[:matches[0].start()].strip()
    lead_lines = [ln for ln in lead.splitlines()
                  if ln.strip() and not ln.strip().startswith("#")]
    if lead_lines:
        sections.append(("user", None, None, "\n".join(lead_lines)))
    for k, match in enumerate(matches):
        role, tool_name, call_id = _parse_header(match.group(2))
        start = match.end()
        end = matches[k + 1].start() if k + 1 < len(matches) else len(text)
        chunk = text[start:end].strip()
        if chunk:
            sections.append((role, tool_name, call_id, chunk))
    return sections


def apply_mirror(text: str, *, messages: list[dict[str, Any]],
                 view: list[dict[str, Any]], protect: int,
                 limit_chars: int = 0,
                 edit_gate: str = "fit") -> ApplyResult:
    """Map an edited mirror back to a replacement view (never touches messages)."""
    include_reasoning = _echo_enabled()
    before = total_chars(view, include_reasoning)
    safe_protect = max(0, min(int(protect), len(messages)))
    protected = [copy.deepcopy(m) for m in messages[:safe_protect]]

    # Fast path: body-identical (mod metadata) means no edit — skip the
    # lossy text->view round-trip entirely so an untouched mirror never
    # rewrites the view.
    rendered_now = render_mirror(view, protect=protect,
                                 limit_chars=limit_chars)
    if _strip_metadata(text) == _strip_metadata(rendered_now):
        return ApplyResult("rejected_noop", before, before, len(view),
                           "mirror matches the live view; nothing to sync")

    body = text if isinstance(text, str) else str(text)
    parsed_sections = _extract_sections(body)
    if parsed_sections is None:
        return ApplyResult("rejected_noop", before, before, len(view),
                           "no turn headers found; view unchanged")
    current_sections = _extract_sections(rendered_now)
    # An empty editable region renders no headers; only prefix-guarding
    # applies then, and a stale-file prefix check is vacuous.
    if current_sections is not None and _is_section_prefix(
            [s[3] for s in parsed_sections],
            [s[3] for s in current_sections]):
        return ApplyResult("rejected_noop", before, before, len(view),
                           "mirror lags the live view (tool output pending); "
                           "nothing to sync")
    sections = parsed_sections

    if not sections:
        # Every body emptied: keep only the protected prefix.
        candidate = list(protected)
        after = total_chars(candidate, include_reasoning)
        gate = _gate_allows(before, after, limit_chars, edit_gate)
        if not gate[0]:
            return ApplyResult("rejected_grew", before, after, len(candidate), gate[1])
        return ApplyResult("applied", before, after, len(candidate),
                           f"mirror sync applied: {before}->{after} chars, "
                           f"{len(candidate)} turns", candidate)

    parsed: list[dict[str, Any]] = []
    for role, tool_name, call_id, chunk in sections:
        if role == "assistant":
            parsed.append({"role": "assistant", "content": chunk})
            continue
        if role == "tool":
            stripped = chunk.lstrip()
            if stripped.startswith(KEEP_MARK) and call_id:
                pair = _find_pair(messages, call_id)
                if pair is None:
                    return ApplyResult(
                        "rejected_broke_pairing", before, before, len(view),
                        f"turn id={call_id} has {KEEP_MARK} but no matching "
                        "assistant tool call; view unchanged")
                assistant_msg, tool_msg = pair
                parsed.append(copy.deepcopy(assistant_msg))
                parsed.append(copy.deepcopy(tool_msg))
                continue
            label = tool_name or "tool"
            ident = f" id={call_id}" if call_id else ""
            parsed.append({"role": "user",
                           "content": f"(archived tool observation "
                                      f"{label}{ident}):\n{chunk}"})
            continue
        parsed.append({"role": "user", "content": chunk})

    merged = _merge_same_role([m for m in parsed if (m.get("content") or "").strip()])
    candidate = protected + merged
    after = total_chars(candidate, include_reasoning)
    gate_ok, gate_note = _gate_allows(before, after, limit_chars, edit_gate)
    if not gate_ok:
        return ApplyResult("rejected_grew", before, after, len(candidate), gate_note)
    # No-op when nothing material changed.
    if after == before and len(candidate) == len(view):
        return ApplyResult("rejected_noop", before, after, len(view),
                           "mirror matches the live view; nothing to sync")
    return ApplyResult("applied", before, after, len(candidate),
                       f"mirror sync applied: {before}->{after} chars, "
                       f"{len(candidate)} turns", candidate)


def _gate_allows(before: int, after: int, limit: int,
                 gate: str) -> tuple[bool, str]:
    if gate == "shrink":
        if after >= before:
            return (False,
                    f"edit rejected by shrink gate: {before}->{after} chars; "
                    "replace stale text with shorter notes")
        return True, ""
    # fit (default): any shrink is kept (even if still over the limit —
    # partial progress beats losing it); growth is kept only while the
    # result still fits.
    if after > before and limit and after > limit:
        return (False,
                f"edit rejected by fit gate: {before}->{after} chars "
                f"exceeds the {limit}-char limit; compact more instead")
    return True, ""


__all__ = ["ApplyResult", "MIRROR_FORMAT_VERSION", "TRIVIAL_SYNC_CHARS",
           "_strip_metadata", "apply_mirror",
           "message_chars", "render_mirror", "total_chars"]
