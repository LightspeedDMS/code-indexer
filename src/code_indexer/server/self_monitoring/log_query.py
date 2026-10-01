"""
Read-only log query entry point for the self-monitoring scan.

This module is the self-monitoring scan's sole Bash command, invoked as:
    <python> -m code_indexer.server.self_monitoring.log_query "<SQL>"

There is no shell in this path at all (Python's sqlite3 module has no
concept of dot-commands), and every read runs through:
  - a `file:<path>?mode=ro` URI open (the OS itself refuses writes to the
    underlying file descriptor for this connection);
  - a `set_authorizer` callback that permits ONLY SQLITE_SELECT,
    SQLITE_READ and SQLITE_FUNCTION action codes (denies ATTACH, DETACH,
    PRAGMA, every DDL/DML action, and anything else), and additionally
    denies the `load_extension` function by name even though SQLITE_FUNCTION
    is otherwise allowed;
  - extension loading that is never enabled (Python's sqlite3 module has it
    off by default; this module never calls enable_load_extension(True));
  - Python's own sqlite3.execute(), which refuses more than one statement on
    its own ("You can only execute one statement at a time.") and never
    executes the second one;
  - a wall-clock deadline enforced via set_progress_handler, so a query with
    no inherent time bound (a self-join, a sorted cross join, ...) is
    aborted rather than running for the scan's entire timeout budget;
  - capped randomblob/zeroblob implementations (registered via
    create_function, overriding the built-ins of the same name), so a
    single call cannot allocate an unbounded amount of memory;
  - a hard cap on the number of rows returned.

The log database path is fixed by the server, never by the agent: it comes
ONLY from the CIDX_LOG_QUERY_DB_PATH environment variable, which
ClaudeInvoker sets for this flow's subprocess. There is no path argument
for the agent to redirect elsewhere.
"""

from __future__ import annotations

import os
import sqlite3
import sys
import time
from typing import List, Optional, Tuple

ENV_VAR_DB_PATH = "CIDX_LOG_QUERY_DB_PATH"

# Generous enough for the scan's own delta-processing queries (which
# already self-limit to 100 rows in their SQL), while still bounding the
# worst case of a query with no LIMIT clause at all.
MAX_ROWS = 200

# Truncate any single field's printed text so one enormous log message
# cannot blow up the agent's context window or the printed output size.
MAX_FIELD_WIDTH = 500

# Wall-clock budget for a single query, independent of what makes it slow
# (a multi-way self-join, a sorted cross join spilling to temp files, ...).
# Well under the scan's overall CLI timeout, so a runaway query fails fast
# and leaves room for the agent to try a narrower one instead.
QUERY_WALL_CLOCK_DEADLINE_SECONDS = 60

# How many sqlite3 VM instructions elapse between progress-handler calls.
# Small enough that the wall-clock check above is not meaningfully delayed;
# large enough not to add measurable per-instruction overhead.
_PROGRESS_HANDLER_VM_INSTRUCTION_INTERVAL = 1000

# Cap for randomblob()/zeroblob() allocation size. This is a log-analysis
# entry point with no legitimate need for large blobs; the cap exists only
# to bound worst-case memory use, not to support any real query shape.
MAX_BLOB_SIZE_BYTES = 1_048_576  # 1 MiB

_ALLOWED_ACTION_CODES = frozenset(
    {
        sqlite3.SQLITE_SELECT,
        sqlite3.SQLITE_READ,
        sqlite3.SQLITE_FUNCTION,
    }
)

# Denied by name even though SQLITE_FUNCTION is otherwise authorized --
# these are SQL functions capable of side effects or filesystem access
# rather than pure data functions.
_DENIED_FUNCTION_NAMES = frozenset({"load_extension"})


class QueryRejected(Exception):
    """Raised for any query this entry point refuses to run, before or
    instead of letting sqlite3 itself see it."""


def _authorizer(action_code, arg1, arg2, db_name, trigger_name):
    """sqlite3 set_authorizer callback: permit only SELECT/read/function.

    arg2 carries the function name for SQLITE_FUNCTION calls (per the
    sqlite3 C API); denying load_extension by name closes it even though
    SQLITE_FUNCTION itself is otherwise authorized for ordinary functions
    like SUBSTR/COUNT that the scan's own queries use.
    """
    if action_code == sqlite3.SQLITE_FUNCTION:
        function_name = (arg2 or "").lower()
        if function_name in _DENIED_FUNCTION_NAMES:
            return sqlite3.SQLITE_DENY
        return sqlite3.SQLITE_OK
    if action_code in _ALLOWED_ACTION_CODES:
        return sqlite3.SQLITE_OK
    return sqlite3.SQLITE_DENY


def _make_capped_blob_function(name: str):
    """Return a create_function-compatible callable implementing `name`
    (either "randomblob" or "zeroblob") with a hard size cap.

    Registering this under the same name via conn.create_function()
    overrides sqlite3's own built-in of that name for this connection.
    """

    def _capped(size):
        if not isinstance(size, int) or size < 0 or size > MAX_BLOB_SIZE_BYTES:
            raise ValueError(
                f"{name}({size!r}) exceeds the {MAX_BLOB_SIZE_BYTES}-byte cap"
            )
        if name == "randomblob":
            return os.urandom(size)
        return bytes(size)

    return _capped


def _make_progress_handler(deadline_monotonic: float):
    """Return a set_progress_handler callback that aborts once the given
    time.monotonic() deadline has passed. Returning non-zero from a
    progress-handler callback interrupts the running query."""

    def _handler() -> int:
        return 1 if time.monotonic() > deadline_monotonic else 0

    return _handler


def _format_value(value: object) -> str:
    text = "" if value is None else str(value)
    if len(text) > MAX_FIELD_WIDTH:
        text = text[:MAX_FIELD_WIDTH] + "...(truncated)"
    return text.replace("\n", "\\n").replace("|", "\\|")


def _run_query(
    db_path: str, sql: str, deadline_seconds: float
) -> Tuple[List[str], List[tuple], bool]:
    """Implementation behind run_query(), with the wall-clock deadline as
    an explicit parameter so tests can exercise it without waiting out the
    real default."""
    if not db_path:
        raise QueryRejected(f"{ENV_VAR_DB_PATH} is not set")
    if not sql.strip():
        raise QueryRejected("empty query")

    uri = f"file:{db_path}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        conn.set_authorizer(_authorizer)
        conn.create_function("randomblob", 1, _make_capped_blob_function("randomblob"))
        conn.create_function("zeroblob", 1, _make_capped_blob_function("zeroblob"))
        deadline = time.monotonic() + deadline_seconds
        conn.set_progress_handler(
            _make_progress_handler(deadline),
            _PROGRESS_HANDLER_VM_INSTRUCTION_INTERVAL,
        )
        # sqlite3's own execute() refuses a second statement on its own
        # (raises sqlite3.Warning, a subclass of Exception rather than of
        # sqlite3.Error) and never executes it.
        cursor = conn.execute(sql)
        columns = [d[0] for d in cursor.description] if cursor.description else []
        rows = cursor.fetchmany(MAX_ROWS + 1)
    finally:
        conn.close()

    truncated = len(rows) > MAX_ROWS
    return columns, rows[:MAX_ROWS], truncated


def run_query(db_path: str, sql: str) -> Tuple[List[str], List[tuple], bool]:
    """Run exactly one read-only SQL statement against db_path.

    Returns (column_names, rows, truncated). Raises QueryRejected when
    db_path or sql is empty, and whatever sqlite3 itself raises (typically
    DatabaseError/OperationalError with "not authorized" when the
    authorizer denies the statement, sqlite3.Warning for a second
    statement, or OperationalError "interrupted" if the wall-clock deadline
    fires).
    """
    return _run_query(db_path, sql, QUERY_WALL_CLOCK_DEADLINE_SECONDS)


def main(argv: Optional[List[str]] = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if len(argv) != 1:
        print(
            'usage: python3 -m code_indexer.server.self_monitoring.log_query "<SQL>"',
            file=sys.stderr,
        )
        return 2

    sql = argv[0]
    db_path = os.environ.get(ENV_VAR_DB_PATH, "")

    try:
        columns, rows, truncated = run_query(db_path, sql)
    except QueryRejected as exc:
        print(f"QUERY REJECTED: {exc}", file=sys.stderr)
        return 1
    except (sqlite3.Error, sqlite3.Warning) as exc:
        print(f"DATABASE ERROR: {exc}", file=sys.stderr)
        return 1

    if columns:
        print("|".join(columns))
    for row in rows:
        print("|".join(_format_value(v) for v in row))
    if truncated:
        print(f"... (truncated at {MAX_ROWS} rows)", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
