"""Schema-driven argument shapes: render what was expected, name what arrived.

A tool's JSON schema is the single source of truth for its argument
shapes.  The model is shown that schema, dispatch enforces it before the
handler runs, and every message is rendered from the same dict, so the
wording can never drift from the constraint it reports.

Two rules keep the messages useful:

* **State the expected shape, not a guess.**  ``shape_of`` renders the
  object's required keys exactly as the schema declares them, so a
  violation is always answered with the shape to send instead.
* **Echo only the structure that arrived.**  ``_arrived`` names key
  names or a JSON type, never a value, and both are bounded.  One field
  can hold a whole file; an error message must not grow with it.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

#: Echo bounds: at most this many key names, each clipped to this length.
MAX_ECHO_KEYS = 8
MAX_ECHO_KEY_CHARS = 40

_TYPE_PHRASES = {
    "string": "a string",
    "integer": "an integer",
    "number": "a number",
    "boolean": "a boolean",
    "object": "an object",
    "array": "an array",
    "null": "null",
}

_PLURALS = {
    "string": "strings",
    "integer": "integers",
    "number": "numbers",
    "boolean": "booleans",
    "object": "objects",
    "array": "arrays",
}

# Ordered so ``bool`` is reported before ``int`` (a subclass of it) and
# ``integer`` before ``number``.
_ARRIVED_CHECKS = ("null", "boolean", "integer", "number", "string", "array")


def matches_type(value: Any, expected: str) -> bool:
    """Whether ``value`` satisfies one JSON-schema ``type`` name."""
    # bool is a subclass of int, so it needs to be checked separately.
    return {
        "object": isinstance(value, dict),
        "array": isinstance(value, list),
        "string": isinstance(value, str),
        "integer": isinstance(value, int) and not isinstance(value, bool),
        "number": isinstance(value, (int, float)) and not isinstance(value, bool),
        "boolean": isinstance(value, bool),
        "null": value is None,
    }.get(expected, True)


def shape_of(schema: Mapping[str, Any]) -> str:
    """Render an object schema's expected shape, e.g. ``{'a': ..., 'b': ...}``."""
    keys = schema.get("required")
    if not isinstance(keys, list) or not keys:
        properties = schema.get("properties")
        keys = list(properties) if isinstance(properties, Mapping) else []
    return "{" + ", ".join(f"{str(key)!r}: ..." for key in keys) + "}"


def _arrived(value: Any) -> str:
    """Name what arrived: key names for objects, a JSON type otherwise."""
    if isinstance(value, Mapping):
        names = sorted(str(key) for key in value)
        shown = [name if len(name) <= MAX_ECHO_KEY_CHARS
                 else name[:MAX_ECHO_KEY_CHARS] + "..."
                 for name in names[:MAX_ECHO_KEYS]]
        parts = [f"{name!r}" for name in shown]
        if len(names) > len(shown):
            parts.append(f"and {len(names) - len(shown)} more")
        return "{" + ", ".join(parts) + "}"
    for name in _ARRIVED_CHECKS:
        if matches_type(value, name):
            return _TYPE_PHRASES[name]
    return type(value).__name__


def _expected_phrase(expected: Any) -> str:
    if isinstance(expected, list):
        phrases = [_TYPE_PHRASES.get(str(name), str(name))
                   for name in expected]
        return " or ".join(phrases) or "a value"
    return _TYPE_PHRASES.get(str(expected), str(expected))


def _type_ok(value: Any, expected: Any) -> bool:
    if isinstance(expected, str):
        return matches_type(value, expected)
    if isinstance(expected, list):
        return any(isinstance(name, str) and matches_type(value, name)
                   for name in expected)
    return True


def _item_phrase(schema: Mapping[str, Any]) -> str:
    """What an array's items should look like, for messages."""
    items = schema.get("items")
    if not isinstance(items, Mapping):
        return "items"
    expected = items.get("type")
    if expected == "object":
        return f"{shape_of(items)} objects"
    if isinstance(expected, str):
        return _PLURALS.get(expected, f"{expected} items")
    return "items"


def value_problem(value: Any, schema: Mapping[str, Any], path: str, *,
                  tool: str) -> str | None:
    """The first way ``value`` violates ``schema``, as a message, or ``None``.

    ``path`` locates the value in the call's arguments (``edits``,
    ``edits[1]``, ``edits[1].old_text``; array positions are 1-based to
    match the handler's "edit 1" wording).  Messages are prefixed with
    ``tool`` so a turn that issues several calls stays unambiguous.
    """
    expected = schema.get("type")
    if not _type_ok(value, expected):
        if expected == "object" or (isinstance(expected, list)
                                    and "object" in expected):
            return (f"{tool}: {path} must be exactly one object "
                    f"{shape_of(schema)}; got {_arrived(value)}")
        if expected == "array":
            return (f"{tool}: {path} must be an array of "
                    f"{_item_phrase(schema)}; got {_arrived(value)}")
        return (f"{tool}: {path} must be {_expected_phrase(expected)}; "
                f"got {_arrived(value)}")
    if isinstance(value, Mapping):
        return _object_problem(value, schema, path, tool=tool)
    if isinstance(value, list):
        return _array_problem(value, schema, path, tool=tool)
    return _scalar_problem(value, schema, path, tool=tool)


def _object_problem(value: Mapping[Any, Any], schema: Mapping[str, Any],
                    path: str, *, tool: str) -> str | None:
    properties = schema.get("properties")
    properties = properties if isinstance(properties, Mapping) else {}
    required = schema.get("required")
    required = required if isinstance(required, list) else []
    unknown_allowed = schema.get("additionalProperties", True) is not False
    missing = [str(key) for key in required if key not in value]
    unknown = [] if unknown_allowed else [str(key) for key in value
                                          if key not in properties]
    if missing or unknown:
        # One sentence for any wrong-shaped object: the shape it must be
        # plus the key names it actually is.  Listing missing and unknown
        # keys separately only invites reading it as a per-key verdict.
        return (f"{tool}: {path} must be exactly one object "
                f"{shape_of(schema)}; got {_arrived(value)}")
    for key, definition in properties.items():
        if key in value and isinstance(definition, Mapping):
            problem = value_problem(value[key], definition,
                                    f"{path}.{key}", tool=tool)
            if problem:
                return problem
    return None


def _array_problem(value: list[Any], schema: Mapping[str, Any], path: str,
                   *, tool: str) -> str | None:
    items = schema.get("items")
    minimum = schema.get("minItems")
    if isinstance(minimum, int) and len(value) < minimum:
        noun = _item_phrase(schema)
        if minimum == 1:
            return f"{tool}: {path} must be a non-empty array of {noun}"
        return f"{tool}: {path} must hold at least {minimum} items"
    if not isinstance(items, Mapping):
        return None
    for index, item in enumerate(value, 1):
        problem = value_problem(item, items, f"{path}[{index}]", tool=tool)
        if problem:
            return problem
    return None


def _scalar_problem(value: Any, schema: Mapping[str, Any], path: str, *,
                    tool: str) -> str | None:
    minimum = schema.get("minLength")
    if not isinstance(minimum, int) or not isinstance(value, str):
        return None
    if len(value) >= minimum:
        return None
    if minimum == 1:
        return f"{tool}: {path} must be a non-empty string"
    return f"{tool}: {path} must be at least {minimum} characters"


def nested_problem(arguments: Mapping[str, Any], parameters: Mapping[str, Any],
                   tool: str) -> str | None:
    """First violation of a *nested* constraint in the arguments, if any.

    The dispatcher reports the top-level contract itself (missing and
    unexpected arguments, a wrong primitive type) in its own wording; this
    walks into the values instead — array items and nested objects - so a
    constraint declared in a schema is enforced from that schema rather
    than re-implemented inside every handler.
    """
    properties = parameters.get("properties")
    if not isinstance(properties, Mapping):
        return None
    for field, value in arguments.items():
        definition = properties.get(field)
        if not isinstance(definition, Mapping):
            continue
        problem = value_problem(value, definition, str(field), tool=tool)
        if problem:
            return problem
    return None


__all__ = ["MAX_ECHO_KEYS", "MAX_ECHO_KEY_CHARS", "matches_type",
           "nested_problem", "shape_of", "value_problem"]
