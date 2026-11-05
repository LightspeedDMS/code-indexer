"""MCP tool argument coercion.

Tool arguments arrive as JSON from any client: a number may come as a string,
or as nothing. Handlers in the core and in features read optional numeric
arguments through these helpers, so a malformed value means the default rather
than an error. Added in SDK 0.7.
"""

from __future__ import annotations

from typing import Any


def coerce_int(value: Any, default: int) -> int:
    """*value* as an int, *default* when it is None or not a number."""
    if value is None:
        return default
    try:
        return int(value)
    except (ValueError, TypeError):
        return default


def coerce_float(value: Any, default: float) -> float:
    """*value* as a float, *default* when it is None or not a number."""
    if value is None:
        return default
    try:
        return float(value)
    except (ValueError, TypeError):
        return default
