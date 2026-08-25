"""
Shared helper for reading columns that are JSONB on PostgreSQL and TEXT on
SQLite (Story #523-family backends, Bug #1622, Bug #1652).

INVARIANT: any column that is JSONB in PostgreSQL and TEXT in SQLite MUST
be read through `parse_json_column()` — never a bare `json.loads()`.
psycopg already deserializes a JSONB column into a native python object
(dict or list) before the row ever reaches application code, while
sqlite3 always returns the TEXT column as a str. Calling `json.loads()`
unconditionally on such a value raises `TypeError` on PostgreSQL.

This module consolidates what used to be three near-identical
implementations of the same "accept a value already of the expected
type as-is, else json.loads() str/bytes, fail-soft on anything
malformed" pattern (Messi Rule #4 anti-duplication, three-strike rule):

  - dependency_map_routes.py's _parse_phase_timings_value (Bug #1622)
  - wiki_cache.py's _parse_json_cache_value (Bug #1652)
  - activated_repo_manager.py's inlined isinstance/json.loads logic in
    _pg_row_to_metadata

All three call sites are wired to import and call `parse_json_column()`
directly rather than keeping their own copy of this logic.
"""

from __future__ import annotations

import json
import logging
from typing import Optional, Type, TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Bug #1652 code-review remediation (F5): never log an entire raw value —
# a JSONB column (e.g. a large wiki sidebar) can be a multi-hundred-KB
# blob, and dumping it whole into logs.db on every malformed/mismatched
# row would bloat the log store for no diagnostic benefit beyond a
# preview of the value.
_MAX_LOGGED_VALUE_LENGTH = 200

# UnicodeDecodeError is already a ValueError subclass (json.loads() on
# malformed bytes raises it while decoding to str internally), listed
# explicitly for clarity even though (ValueError, TypeError) alone
# already catches it.
_MALFORMED_JSON_EXCEPTIONS = (ValueError, TypeError, UnicodeDecodeError)


def parse_json_column(
    raw: object, expected_type: Type[T], field_name: str
) -> Optional[T]:
    """
    Normalize a DB row's raw JSON-bearing value to `expected_type`.

    A value already of `expected_type` is returned as-is (no parsing, no
    warning) — this is the PostgreSQL JSONB case. Any other value is
    passed to `json.loads()` — this is the SQLite TEXT case (also accepts
    bytes). A value that is neither `expected_type` nor str/bytes, or a
    string/bytes value that fails to parse (including invalid UTF-8
    bytes), or one that parses to something other than `expected_type`,
    logs a WARNING (with the raw value's repr() truncated to
    `_MAX_LOGGED_VALUE_LENGTH` chars) and returns None — fail-soft, never
    raise.

    Args:
        raw: The row's raw value (None, `expected_type` instance, str,
            bytes, or an unexpected type).
        expected_type: The python type the value should normalize to
            (e.g. ``dict`` for a metadata/phase-timings column, ``list``
            for a sidebar/array column). Must be an actual ``type``.
        field_name: Human-readable field name used in WARNING log
            messages (e.g. "sidebar_json", "phase_timings_json").

    Returns:
        The parsed value of `expected_type`, or None if raw was None or
        malformed.

    Raises:
        TypeError: if `expected_type` is not an actual type (programmer
            error at the call site, not a data-quality issue — this is
            NOT the fail-soft path).
    """
    if not isinstance(expected_type, type):
        raise TypeError(
            f"parse_json_column: expected_type must be a type, got "
            f"{expected_type!r} ({type(expected_type).__name__})"
        )
    if raw is None:
        # Absent value — no warning; expected for legacy or NULL rows.
        return None
    if isinstance(raw, expected_type):
        return raw
    try:
        # raw is intentionally untyped (object) at this point: json.loads()
        # only accepts str/bytes/bytearray and raises TypeError for
        # anything else (e.g. an int). That real TypeError is caught
        # immediately below and turned into the graceful None+WARNING
        # fail-soft path, and its message text is what the WARNING log
        # reports — narrowing raw via isinstance(raw, (str, bytes)) first
        # would require synthesizing a different message for the
        # non-str/bytes case instead of reusing json.loads()'s own
        # diagnostic text, so the broader "object" signature is
        # deliberate rather than a missed narrowing opportunity.
        parsed = json.loads(raw)  # type: ignore[arg-type]
    except _MALFORMED_JSON_EXCEPTIONS as exc:
        logger.warning(
            "parse_json_column: malformed %s value %s: %s",
            field_name,
            repr(raw)[:_MAX_LOGGED_VALUE_LENGTH],
            exc,
        )
        return None
    if not isinstance(parsed, expected_type):
        logger.warning(
            "parse_json_column: %s parsed to %s (expected %s), ignoring value %s",
            field_name,
            type(parsed).__name__,
            expected_type.__name__,
            repr(raw)[:_MAX_LOGGED_VALUE_LENGTH],
        )
        return None
    return parsed
