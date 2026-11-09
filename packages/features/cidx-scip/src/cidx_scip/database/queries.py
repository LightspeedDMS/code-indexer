"""SCIP database query operations for symbol lookup and reference search."""

try:
    from pysqlite3 import dbapi2 as sqlite3
except ImportError:
    import sqlite3

import logging
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple, cast
from .builder import ROLE_DEFINITION, ROLE_IMPORT, ROLE_WRITE_ACCESS, ROLE_READ_ACCESS

logger = logging.getLogger(__name__)


class QueryTimeoutError(Exception):
    """Raised when a query exceeds the timeout limit."""

    pass


# Cap max_depth at 3 for trace_call_chain_v2 / trace_call_chain_v2_batched
# (Story #609; Bug #1603 makes this the ADVERTISED contract too, not just
# the internal safety cap).
#
# These two functions do NOT find the shortest path -- they enumerate ALL
# distinct paths up to max_depth via a recursive CTE with
# string-concatenation path tracking, paired with a `backward_reachable`
# CTE of equal depth bound. Path count for a call graph with branching
# factor b grows roughly as O(b^depth) -- even a modest branching factor
# (3-5) makes depth 10 combinatorially explosive against a real
# ~100K-symbol SCIP index, which is exactly the performance concern
# Story #609 already flagged ("to prevent performance degradation"). Depth
# 3 is the value that has actually been exercised safely in production
# since Story #609; there is no evidence depths 4-10 are safe, and the
# combinatorial structure of the query argues they are not. Bug #1603
# tightens the CONTRACT (scip_callchain's advertised [1, 10] max_depth) to
# match this REAL cap, rather than raising the real cap to match an
# unproven advertised contract -- see server/mcp/handlers/scip.py and
# server/routers/scip_queries.py for the corresponding [1, 3] clamp.
MAX_DEPTH_CAP = 3


def _resolve_db_path(conn: sqlite3.Connection) -> str:
    """Resolve the absolute on-disk database file path from an open connection.

    Needed by the watchdog-thread timeout pattern used by
    trace_call_chain_v2 / trace_call_chain_v2_batched (Bug #1603): those
    functions must run their query on a BRAND NEW sqlite3 connection opened
    INSIDE the spawned watchdog thread, because Python's sqlite3 module
    hard-enforces that a connection may only be used from the thread that
    created it (the caller's `conn` was created on the calling/request
    thread, and the production DatabaseBackend does not pass
    check_same_thread=False). PRAGMA database_list's third column is the
    absolute file path for the 'main' database of a file-backed connection.
    """
    row = conn.execute("PRAGMA database_list").fetchone()
    path = row[2] if row is not None else ""
    if not path:
        raise ValueError(
            "_resolve_db_path requires a file-backed sqlite3 connection "
            "(PRAGMA database_list returned no path -- e.g. ':memory:' "
            "or a connection with no 'main' database attached)."
        )
    return cast(str, path)


class _ConnectionHolder:
    """Thread-safe one-slot holder for a worker's live sqlite3 connection.

    Unlike the plain-list result_holder/exc_holder pattern below (which is
    only ever read AFTER t.join() returns -- a safe happens-before point),
    this holder's `.interrupt()` is called WHILE the worker thread may
    still be running, so publish/read must be lock-protected (Bug #1603
    code review Priority 1 concurrency fix).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._conn: Optional["sqlite3.Connection"] = None
        self._cancelled = False

    def publish(self, conn: "sqlite3.Connection") -> None:
        """Called by the worker thread immediately after connecting."""
        with self._lock:
            self._conn = conn

    def is_cancelled(self) -> bool:
        """True once interrupt() has fired, regardless of publish timing.

        Bug #1603 code review round 4 (Codex): sqlite3's conn.interrupt()
        only affects a statement already executing -- it has NO effect on
        one that starts AFTER it is called. If the watchdog's timeout
        fires before the worker has even connected (self._conn is None
        below), interrupt() correctly no-ops on the connection, but that
        alone leaves the worker with no way to know it should stop once
        it does connect. This flag closes that window: checked by the
        impl functions immediately after publish(), so a timeout that
        fires arbitrarily early still stops the expensive query from
        running to completion uncancelled.
        """
        with self._lock:
            return self._cancelled

    def interrupt(self, warning_label: str) -> None:
        """Cancel the published connection's in-flight query, if any, and
        record that a timeout occurred regardless (see is_cancelled()).

        Never raises: the worker may have already finished and closed its
        own connection in the tiny window since the caller's is_alive()
        check, or may not have published yet (extremely tight timeout).
        """
        with self._lock:
            self._cancelled = True
            conn = self._conn
        if conn is None:
            return
        try:
            conn.interrupt()
        except Exception as interrupt_exc:
            logger.debug(
                f"{warning_label}: conn.interrupt() on timeout failed "
                f"(worker likely already finished): {interrupt_exc}"
            )


def _run_with_thread_watchdog(
    work: Callable[[], List[Dict[str, Any]]],
    timeout_seconds: float,
    warning_label: str,
    conn_holder: Optional[_ConnectionHolder] = None,
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Run `work` on a daemon thread with a join-based timeout.

    signal.alarm() only delivers SIGALRM to the main thread of the main
    interpreter -- it raises ValueError when called from any other thread,
    which is exactly how every real server request executes (uvicorn/
    FastAPI worker threads). threading.Thread.join(timeout=...) has no such
    restriction, so it replaces signal.alarm() here (mirrors
    get_smart_context in scip/query/composites.py).

    A timeout is reported as a normal (results, error_message) return, not
    an exception -- preserving the pre-existing public contract. `conn_holder`
    (optional; `work` stays a zero-arg callable) lets the caller enable real
    cancellation on timeout via its thread-safe .interrupt().
    """
    result_holder: List[List[Dict[str, Any]]] = [[]]
    exc_holder: List[Optional[BaseException]] = [None]

    def _target() -> None:
        try:
            result_holder[0] = work()
        except BaseException as e:
            exc_holder[0] = e

    t = threading.Thread(target=_target, daemon=True, name=f"{warning_label}-watchdog")
    t.start()
    t.join(timeout=timeout_seconds if timeout_seconds > 0 else None)

    if t.is_alive():
        error_msg = (
            f"Query exceeded {timeout_seconds}-second timeout. "
            "Try reducing depth or narrowing search."
        )
        logger.warning(f"{warning_label} timeout: {error_msg}")
        if conn_holder is not None:
            conn_holder.interrupt(warning_label)
        return [], error_msg

    if exc_holder[0] is not None:
        raise exc_holder[0]

    return result_holder[0], None


def _determine_relationship(role: int) -> str:
    """Map SCIP role flags to relationship type."""
    if role & ROLE_IMPORT:
        return "import"
    elif role & ROLE_WRITE_ACCESS:
        return "write"
    elif role & ROLE_READ_ACCESS:
        return "call"
    return "reference"


def find_definition(
    conn: sqlite3.Connection, symbol_name: str, exact: bool = False
) -> List[Dict[str, Any]]:
    """
    Find definition locations for a symbol using FTS5-optimized SQL query.

    Uses FTS5 symbols_fts table for fast symbol name lookup, eliminating
    full table scans and achieving <5ms performance on production datasets.

    Args:
        conn: SQLite database connection
        symbol_name: Symbol name to search for (e.g., "TestClass", "authenticate")
        exact: If True, match exact symbol name; if False, match substring (FTS5 MATCH)

    Returns:
        List of dictionaries with keys:
            - symbol_name: Full SCIP symbol identifier
            - file_path: Relative file path
            - line: Line number (0-indexed)
            - column: Column number (0-indexed)
            - kind: Symbol kind (Class, Method, etc.)
            - role: Role bitmask
    """
    cursor = conn.cursor()

    # Sanitize input for FTS5 (escape double quotes)
    safe_symbol_name = symbol_name.replace('"', '""')

    if exact:
        # Check if this is a full SCIP symbol path (starts with language prefix)
        # Full SCIP symbols start with: "python ", "java ", "typescript ", etc.
        is_full_scip_symbol = any(
            symbol_name.startswith(prefix)
            for prefix in [
                "python ",
                "java ",
                "typescript ",
                "go ",
                "rust ",
                "cpp ",
                "csharp ",
                "ruby ",
            ]
        )

        if is_full_scip_symbol:
            # Use direct equality match for full SCIP symbols
            query = """
                SELECT
                    s.name as symbol_name,
                    d.relative_path as file_path,
                    o.start_line as line,
                    o.start_char as column,
                    s.kind as kind,
                    o.role as role
                FROM symbols s
                JOIN occurrences o ON o.symbol_id = s.id
                JOIN documents d ON o.document_id = d.id
                WHERE s.name = ?
                    AND (o.role & 1) = 1
                ORDER BY d.relative_path, o.start_line
            """
            cursor.execute(query, (symbol_name,))
        else:
            # Use FTS5 for fast symbol name lookup combined with LIKE for exact suffix matching
            # FTS5 MATCH query returns matching symbol IDs instantly
            # LIKE filters to exact symbol definitions based on format:
            #   - Class: /ClassName# (exact)
            #   - Method: /ClassName#method(). or /ClassName#method() (allow both formats)
            #   - Attribute: /ClassName#attr.
            query = """
                SELECT
                    s.name as symbol_name,
                    d.relative_path as file_path,
                    o.start_line as line,
                    o.start_char as column,
                    s.kind as kind,
                    o.role as role
                FROM symbols_fts fts
                JOIN symbols s ON fts.rowid = s.id
                JOIN occurrences o ON o.symbol_id = s.id
                JOIN documents d ON o.document_id = d.id
                WHERE fts.name MATCH ?
                    AND s.name LIKE ?
                    AND (o.role & 1) = 1
                ORDER BY d.relative_path, o.start_line
            """

            # Determine SCIP format based on symbol_name
            if "#" in symbol_name:
                # Method or attribute query: ClassName#method or ClassName#attr
                # SCIP format: .../ClassName#method(). or .../ClassName#attr.
                # Handle both ClassName#method and ClassName#method() input formats
                if symbol_name.endswith("()"):
                    # User provided ClassName#method() format
                    base = symbol_name[:-2]  # Remove ()
                else:
                    # User provided ClassName#method format
                    base = symbol_name

                # FTS5 pattern for fast filtering
                fts_pattern = f'"/{safe_symbol_name}"'
                # LIKE pattern matches method/attribute format
                # Match both /ClassName#method(). and /ClassName#method().X patterns
                like_pattern = f"%/{base}()%"
            else:
                # Class query: ClassName
                # SCIP format: .../ClassName# (exact, no method/attribute suffix)
                # FTS5 MATCH pattern for fast filtering
                fts_pattern = f'"/{safe_symbol_name}#"'
                # LIKE pattern for exact suffix match (class definition only)
                like_pattern = f"%/{symbol_name}#"

            cursor.execute(query, (fts_pattern, like_pattern))
    else:
        # Fall back to LIKE for substring matching (acceptable for pattern queries)
        # FTS5 MATCH doesn't support true substring matching (requires token boundaries)
        # LIKE is slower but still acceptable for fuzzy queries (not critical path)
        query = """
            SELECT
                s.name as symbol_name,
                d.relative_path as file_path,
                o.start_line as line,
                o.start_char as column,
                s.kind as kind,
                o.role as role
            FROM symbols s
            JOIN occurrences o ON o.symbol_id = s.id
            JOIN documents d ON o.document_id = d.id
            WHERE s.name LIKE ?
                AND (o.role & 1) = 1
            ORDER BY d.relative_path, o.start_line
        """
        # LIKE pattern for substring match
        cursor.execute(query, (f"%{symbol_name}%",))

    # Fetch all results and convert to dictionaries
    results = []
    for row in cursor.fetchall():
        results.append(
            {
                "symbol_name": row[0],
                "file_path": row[1],
                "line": row[2],
                "column": row[3],
                "kind": row[4],
                "role": row[5],
            }
        )

    return results


def _rows_to_call_chain_results(
    rows: List[Tuple[str, str, int, int]],
) -> List[Dict[str, Any]]:
    """Convert raw (path_symbols_str, path_ids_str, depth, has_cycle) rows
    from either call-chain recursive CTE into the public result-dict shape.

    Shared by both the batched and non-batched impl functions since their
    result row shape and mapping are identical.
    """
    results = []
    for path_symbols_str, _path_ids_str, depth, has_cycle in rows:
        results.append(
            {
                "path": path_symbols_str.split("|||"),
                "length": depth,  # Number of hops/edges, not nodes
                "has_cycle": bool(has_cycle),
            }
        )
    return results


# Batched bidirectional-BFS call-chain query (Story #610): accepts lists of
# source/target symbol IDs (via the batch_from_ids/batch_to_ids temp tables
# populated by _trace_call_chain_v2_batched_impl) and finds chains from ANY
# source to ANY target in a single query.
_BATCHED_CALL_CHAIN_QUERY = """
    WITH RECURSIVE
    -- Phase 0a: Expand source symbols (class -> class + methods) from temp table
    source_symbols(symbol_id) AS (
        SELECT id FROM batch_from_ids
        UNION
        SELECT s.id
        FROM symbols s, symbols s_src
        JOIN batch_from_ids bf ON s_src.id = bf.id
        WHERE s_src.name LIKE '%#'
          AND s_src.name NOT LIKE '%()%'
          AND s.name LIKE s_src.name || '%'
          AND s.name LIKE '%()%'
    ),

    -- Phase 0b: Expand target symbols (class -> class + methods) from temp table
    target_symbols(symbol_id) AS (
        SELECT id FROM batch_to_ids
        UNION
        SELECT s.id
        FROM symbols s, symbols s_tgt
        JOIN batch_to_ids bt ON s_tgt.id = bt.id
        WHERE s_tgt.name LIKE '%#'
          AND s_tgt.name NOT LIKE '%()%'
          AND s.name LIKE s_tgt.name || '%'
          AND s.name LIKE '%()%'
    ),

    -- Phase 1: Backward reachability from ALL target symbols
    backward_reachable(symbol_id, depth) AS (
        SELECT symbol_id, 0 FROM target_symbols
        UNION
        SELECT DISTINCT sr.from_symbol_id, br.depth + 1
        FROM backward_reachable br
        JOIN symbol_references sr ON sr.to_symbol_id = br.symbol_id
        WHERE br.depth < ?
    ),

    -- Phase 2: Forward BFS from ALL source symbols with pruning
    forward_paths(symbol_id, path_ids, path_symbols, depth, has_cycle) AS (
        -- Base: all source symbols
        SELECT
            ss.symbol_id,
            CAST(ss.symbol_id AS TEXT),
            (SELECT name FROM symbols WHERE id = ss.symbol_id),
            0,
            0
        FROM source_symbols ss

        UNION

        -- Recursive: explore only backward-reachable symbols
        SELECT
            sr.to_symbol_id,
            fp.path_ids || ',' || sr.to_symbol_id,
            fp.path_symbols || '|||' || s.name,
            fp.depth + 1,
            CASE
                WHEN instr(',' || fp.path_ids || ',', ',' || CAST(sr.to_symbol_id AS TEXT) || ',') > 0
                THEN 1 ELSE 0
            END
        FROM forward_paths fp
        JOIN symbol_references sr ON fp.symbol_id = sr.from_symbol_id
        JOIN symbols s ON sr.to_symbol_id = s.id
        WHERE fp.depth < ?
          AND fp.has_cycle = 0
          -- CRITICAL PRUNING: Only explore nodes that can reach target
          AND sr.to_symbol_id IN (SELECT symbol_id FROM backward_reachable)
    )

    -- Phase 3: Extract paths that reached ANY target symbol
    SELECT DISTINCT path_symbols, path_ids, depth, has_cycle
    FROM forward_paths fp
    WHERE fp.symbol_id IN (SELECT symbol_id FROM target_symbols)
    ORDER BY depth
"""


def trace_call_chain_v2_batched(
    conn: sqlite3.Connection,
    from_symbol_ids: List[int],
    to_symbol_ids: List[int],
    max_depth: int = 3,
    limit: int = 100,
    timeout_seconds: float = 30,
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Trace call chains from any source ID to any target ID via bidirectional BFS on symbol_references (Story #610).

    Args:
        conn: SQLite connection (used only to resolve db_path)
        from_symbol_ids: Entry point symbol IDs
        to_symbol_ids: Target symbol IDs
        max_depth: Path length bound. Values above MAX_DEPTH_CAP (3) are
            clamped down to it; values below 1 are rejected. The recursive
            CTE enumerates ALL distinct paths, not shortest-path, so higher
            depths are combinatorially unsafe (Bug #1603).
        limit: Maximum paths to return (0 = unlimited)
        timeout_seconds: Max execution seconds; fractional values allowed

    Returns:
        (results, error_message) -- results are path/length/has_cycle dicts.
    """
    if max_depth > MAX_DEPTH_CAP:
        logger.warning(
            f"Requested max_depth {max_depth} exceeds cap of {MAX_DEPTH_CAP}. "
            f"Capping at {MAX_DEPTH_CAP} to prevent performance degradation."
        )
        max_depth = MAX_DEPTH_CAP

    if max_depth < 1:
        raise ValueError(f"Max depth must be at least 1, got {max_depth}")

    if not from_symbol_ids or not to_symbol_ids:
        return [], None

    db_path = _resolve_db_path(conn)
    conn_holder = _ConnectionHolder()

    def _work() -> List[Dict[str, Any]]:
        return _trace_call_chain_v2_batched_impl(
            db_path, from_symbol_ids, to_symbol_ids, max_depth, limit, conn_holder
        )

    return _run_with_thread_watchdog(
        _work, timeout_seconds, "trace_call_chain_v2_batched", conn_holder
    )


def _trace_call_chain_v2_batched_impl(
    db_path: str,
    from_symbol_ids: List[int],
    to_symbol_ids: List[int],
    max_depth: int,
    limit: int,
    conn_holder: Optional[_ConnectionHolder] = None,
) -> List[Dict[str, Any]]:
    """Runs the batched bidirectional-BFS query on its OWN sqlite3 connection.

    Executed entirely inside the watchdog thread spawned by
    trace_call_chain_v2_batched -- opens a brand-new connection to the same
    on-disk file so it never touches the caller's connection object
    (sqlite3 connections are single-thread-only unless
    check_same_thread=False was passed at connect() time, which the
    production DatabaseBackend does not do). Publishes the new connection
    into `conn_holder` (if given) immediately so the watchdog can cancel it
    on timeout (Bug #1603 code review Priority 1).
    """
    conn = sqlite3.connect(db_path)
    if conn_holder is not None:
        conn_holder.publish(conn)
        if conn_holder.is_cancelled():
            # Bug #1603 code review round 4 (Codex): the watchdog's
            # timeout already fired before we even connected -- bail out
            # now rather than running the expensive query to completion
            # fully uncancelled (conn.interrupt() has no effect on a
            # statement that hasn't started yet).
            conn.close()
            return []
    try:
        cursor = conn.cursor()

        # Create + populate temp tables for source/target IDs (Story #610)
        cursor.execute(
            "CREATE TEMP TABLE IF NOT EXISTS batch_from_ids (id INTEGER PRIMARY KEY)"
        )
        cursor.execute(
            "CREATE TEMP TABLE IF NOT EXISTS batch_to_ids (id INTEGER PRIMARY KEY)"
        )
        cursor.executemany(
            "INSERT OR IGNORE INTO batch_from_ids VALUES (?)",
            [(id,) for id in from_symbol_ids],
        )
        cursor.executemany(
            "INSERT OR IGNORE INTO batch_to_ids VALUES (?)",
            [(id,) for id in to_symbol_ids],
        )

        query = _BATCHED_CALL_CHAIN_QUERY
        params: List[Any] = [max_depth, max_depth]
        if limit > 0:
            query += " LIMIT ?"
            params.append(limit)

        cursor.execute(query, tuple(params))
        return _rows_to_call_chain_results(cursor.fetchall())
    finally:
        conn.close()


# Single bidirectional-BFS call-chain query: takes one from_symbol_id and
# one to_symbol_id directly as bound parameters (no temp tables needed --
# that's the batched variant's job). Expands class IDs to their methods via
# the source_symbols/target_symbols CTEs below.
_SINGLE_CALL_CHAIN_QUERY = """
    WITH RECURSIVE
    -- Phase 0a: Expand source symbol (class -> class + methods)
    source_symbols(symbol_id) AS (
        SELECT ? AS symbol_id  -- Always include the source ID itself
        UNION
        SELECT s.id
        FROM symbols s, symbols s_src
        WHERE s_src.id = ?
          -- Check if source is a class (ends with #, no parentheses)
          AND s_src.name LIKE '%#'
          AND s_src.name NOT LIKE '%()%'
          -- If it's a class, find all methods (name starts with class name, contains ())
          AND s.name LIKE s_src.name || '%'
          AND s.name LIKE '%()%'
    ),

    -- Phase 0b: Expand target symbol (class -> class + methods)
    target_symbols(symbol_id) AS (
        SELECT ? AS symbol_id  -- Always include the target ID itself
        UNION
        SELECT s.id
        FROM symbols s, symbols s_tgt
        WHERE s_tgt.id = ?
          -- Check if target is a class (ends with #, no parentheses)
          AND s_tgt.name LIKE '%#'
          AND s_tgt.name NOT LIKE '%()%'
          -- If it's a class, find all methods (name starts with class name, contains ())
          AND s.name LIKE s_tgt.name || '%'
          AND s.name LIKE '%()%'
    ),

    -- Phase 1: Backward reachability from ALL target symbols
    backward_reachable(symbol_id, depth) AS (
        SELECT symbol_id, 0 FROM target_symbols
        UNION
        SELECT DISTINCT sr.from_symbol_id, br.depth + 1
        FROM backward_reachable br
        JOIN symbol_references sr ON sr.to_symbol_id = br.symbol_id
        WHERE br.depth < ?
    ),

    -- Phase 2: Forward BFS from ALL source symbols with pruning
    forward_paths(symbol_id, path_ids, path_symbols, depth, has_cycle) AS (
        -- Base: all source symbols
        SELECT
            ss.symbol_id,
            CAST(ss.symbol_id AS TEXT),
            (SELECT name FROM symbols WHERE id = ss.symbol_id),
            0,
            0
        FROM source_symbols ss

        UNION

        -- Recursive: explore only backward-reachable symbols
        SELECT
            sr.to_symbol_id,
            fp.path_ids || ',' || sr.to_symbol_id,
            fp.path_symbols || '|||' || s.name,
            fp.depth + 1,
            CASE
                WHEN instr(',' || fp.path_ids || ',', ',' || CAST(sr.to_symbol_id AS TEXT) || ',') > 0
                THEN 1 ELSE 0
            END
        FROM forward_paths fp
        JOIN symbol_references sr ON fp.symbol_id = sr.from_symbol_id
        JOIN symbols s ON sr.to_symbol_id = s.id
        WHERE fp.depth < ?
          AND fp.has_cycle = 0
          -- CRITICAL PRUNING: Only explore nodes that can reach target
          AND sr.to_symbol_id IN (SELECT symbol_id FROM backward_reachable)
    )

    -- Phase 3: Extract paths that reached ANY target symbol
    SELECT DISTINCT path_symbols, path_ids, depth, has_cycle
    FROM forward_paths fp
    WHERE fp.symbol_id IN (SELECT symbol_id FROM target_symbols)
    ORDER BY depth
"""


def trace_call_chain_v2(
    conn: sqlite3.Connection,
    from_symbol_id: int,
    to_symbol_id: int,
    max_depth: int = 5,
    limit: int = 100,
    timeout_seconds: float = 30,
) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """Trace call chains from from_symbol_id to to_symbol_id via bidirectional BFS on symbol_references (class IDs auto-expand to their methods).

    Args:
        conn: SQLite connection (used only to resolve db_path)
        from_symbol_id: Entry point symbol ID (class or method)
        to_symbol_id: Target symbol ID (class or method)
        max_depth: Path length bound. Values above MAX_DEPTH_CAP (3) are
            clamped down to it; values below 1 are rejected. The recursive
            CTE enumerates ALL distinct paths, not shortest-path, so higher
            depths are combinatorially unsafe (Bug #1603).
        limit: Maximum paths to return (0 = unlimited)
        timeout_seconds: Max execution seconds; fractional values allowed

    Returns:
        (results, error_message) -- results are path/length/has_cycle dicts.
    """
    if max_depth > MAX_DEPTH_CAP:
        logger.warning(
            f"Requested max_depth {max_depth} exceeds cap of {MAX_DEPTH_CAP}. "
            f"Capping at {MAX_DEPTH_CAP} to prevent performance degradation."
        )
        max_depth = MAX_DEPTH_CAP

    if max_depth < 1:
        raise ValueError(f"Max depth must be at least 1, got {max_depth}")

    db_path = _resolve_db_path(conn)
    conn_holder = _ConnectionHolder()

    def _work() -> List[Dict[str, Any]]:
        return _trace_call_chain_v2_impl(
            db_path, from_symbol_id, to_symbol_id, max_depth, limit, conn_holder
        )

    return _run_with_thread_watchdog(
        _work, timeout_seconds, "trace_call_chain_v2", conn_holder
    )


def _trace_call_chain_v2_impl(
    db_path: str,
    from_symbol_id: int,
    to_symbol_id: int,
    max_depth: int,
    limit: int,
    conn_holder: Optional[_ConnectionHolder] = None,
) -> List[Dict[str, Any]]:
    """Runs the single-pair bidirectional-BFS query on its OWN sqlite3
    connection.

    Executed entirely inside the watchdog thread spawned by
    trace_call_chain_v2 -- see _trace_call_chain_v2_batched_impl's
    docstring for why a brand-new connection is required here, and for
    publishing it into `conn_holder` (Bug #1603 code review Priority 1).
    """
    conn = sqlite3.connect(db_path)
    if conn_holder is not None:
        conn_holder.publish(conn)
        if conn_holder.is_cancelled():
            # Bug #1603 code review round 4 (Codex): the watchdog's
            # timeout already fired before we even connected -- bail out
            # now rather than running the expensive query to completion
            # fully uncancelled (conn.interrupt() has no effect on a
            # statement that hasn't started yet).
            conn.close()
            return []
    try:
        cursor = conn.cursor()

        query = _SINGLE_CALL_CHAIN_QUERY
        params: List[Any] = [
            from_symbol_id,
            from_symbol_id,
            to_symbol_id,
            to_symbol_id,
            max_depth,
            max_depth,
        ]
        if limit > 0:
            query += " LIMIT ?"
            params.append(limit)

        cursor.execute(query, tuple(params))
        return _rows_to_call_chain_results(cursor.fetchall())
    finally:
        conn.close()


def trace_call_chain(
    conn: sqlite3.Connection,
    from_symbol_id: int,
    to_symbol_id: int,
    max_depth: int = 5,
    limit: int = 100,
    scip_file: Optional[Path] = None,
    timeout_seconds: float = 30,
) -> List[Dict[str, Any]]:
    """
    Trace all call chains from entry point to target function.

    Auto-detects if call_graph table exists and uses fast recursive CTE
    (trace_call_chain_v2, 0.5ms-5ms) if available. Falls back to BFS hybrid
    approach (slow, 10-100s) for legacy databases without call_graph.

    Performance improvement: 10000x faster with call_graph table.

    Args:
        conn: SQLite database connection
        from_symbol_id: Entry point symbol ID
        to_symbol_id: Target function symbol ID
        max_depth: Maximum path length (1-10)
        limit: Maximum number of paths to return
        scip_file: Optional path to .scip file for hybrid mode (legacy fallback only)
        timeout_seconds: Forwarded to trace_call_chain_v2's watchdog (default 30,
            matching trace_call_chain_v2's own default)

    Returns:
        List of dicts with keys:
            - path: List of symbol names in execution order
            - length: Number of hops
            - has_cycle: Boolean indicating cycle presence

    Raises:
        QueryTimeoutError: If the fast-path query (trace_call_chain_v2) is
            cut off by the watchdog before completing. Bug #1603 code
            review round 2 Priority 2: this legacy helper used to log a
            WARNING and then return the (empty/partial) results anyway,
            making a genuine timeout indistinguishable from "no chains
            found" for any caller. It is now raised instead of swallowed,
            matching the QueryTimeoutError contract already established
            elsewhere in this module family (scip/query/composites.py).
    """
    if max_depth < 1 or max_depth > 10:
        raise ValueError(f"Max depth must be between 1 and 10, got {max_depth}")

    # Auto-detect call_graph table (fast path)
    cursor = conn.cursor()
    cursor.execute(
        """
        SELECT name FROM sqlite_master
        WHERE type='table' AND name='call_graph'
    """
    )
    has_call_graph_table = cursor.fetchone() is not None

    # Check if table exists AND has data
    has_call_graph = False
    if has_call_graph_table:
        cursor.execute("SELECT COUNT(*) FROM call_graph LIMIT 1")
        count = cursor.fetchone()[0]
        has_call_graph = count > 0

    if has_call_graph:
        # Fast path: Use bidirectional BFS on call_graph
        results, error_msg = trace_call_chain_v2(
            conn,
            from_symbol_id,
            to_symbol_id,
            max_depth,
            limit,
            timeout_seconds=timeout_seconds,
        )
        if error_msg:
            logger.warning(f"trace_call_chain timeout: {error_msg}")
            raise QueryTimeoutError(error_msg)
        return results

    # Legacy fallback: BFS hybrid approach (slow, for databases without symbol_references)
    from collections import deque

    cursor = conn.cursor()

    # Get starting and target symbol names for matching
    cursor.execute("SELECT name FROM symbols WHERE id = ?", (from_symbol_id,))
    from_row = cursor.fetchone()
    if not from_row:
        return []
    from_symbol_name = from_row[0]

    cursor.execute("SELECT name FROM symbols WHERE id = ?", (to_symbol_id,))
    to_row = cursor.fetchone()
    if not to_row:
        return []
    to_symbol_name = to_row[0]

    # Simplify starting symbol name
    from_simple_name = (
        from_symbol_name.split("/")[-1].rstrip("#").rstrip(".").rstrip("()")
    )
    to_simple_name = to_symbol_name.split("/")[-1].rstrip("#").rstrip(".").rstrip("()")

    # BFS with hybrid queries
    # Queue contains: (current_symbol_id, path_so_far, visited_set)
    queue = deque([(from_symbol_id, [from_simple_name], {from_symbol_id})])
    chains: List[Dict[str, Any]] = []
    MAX_CHAINS = limit
    MAX_NODES_EXPLORED = 3000  # Prevent BFS explosion, keep <2s performance
    nodes_explored = 0

    while queue and len(chains) < MAX_CHAINS and nodes_explored < MAX_NODES_EXPLORED:
        current_id, path, visited = queue.popleft()
        nodes_explored += 1

        # Check depth
        if len(path) > max_depth:
            continue

        # Get dependencies using HYBRID (ALL symbols)
        deps = get_dependencies(conn, current_id, depth=1, scip_file=scip_file)

        for dep in deps:
            # Early termination if we have enough chains
            if len(chains) >= MAX_CHAINS:
                break

            # Find dep symbol ID
            cursor.execute(
                "SELECT id, name, kind FROM symbols WHERE name = ?",
                (dep["symbol_name"],),
            )
            dep_row = cursor.fetchone()
            if not dep_row:
                continue
            dep_id, dep_name, dep_kind = dep_row

            # Skip parameters and locals (noise that explodes BFS)
            if dep_kind in ("Parameter", "Local") or dep_name.startswith("local "):
                continue

            # Extract simplified symbol name (last part after /)
            dep_simple_name = (
                dep_name.split("/")[-1].rstrip("#").rstrip(".").rstrip("()")
            )

            # Skip if already in path (simple name check to avoid cycles)
            if dep_simple_name in path:
                continue

            # Check if reached target (fuzzy match on simple name)
            if dep_simple_name == to_simple_name or to_simple_name in dep_simple_name:
                # Found a chain!
                full_path = path + [dep_simple_name]
                chains.append(
                    {
                        "path": full_path,
                        "length": len(full_path) - 1,  # Number of hops/edges, not nodes
                        "has_cycle": False,
                    }
                )
                continue

            # Cycle detection
            if dep_id in visited:
                continue

            # Add to queue
            new_visited = visited | {dep_id}
            queue.append((dep_id, path + [dep_simple_name], new_visited))

    # Sort by length
    return sorted(chains, key=lambda c: c["length"])


def find_references(
    conn: sqlite3.Connection,
    symbol_name: str,
    limit: int = 100,
    role_filter: Optional[int] = None,
    exact: bool = True,
) -> List[Dict[str, Any]]:
    """
    Find all references to a symbol using FTS5-optimized SQL query.

    Uses FTS5 symbols_fts table for fast symbol name lookup, eliminating
    full table scans and achieving <10ms performance on production datasets.

    Args:
        conn: SQLite database connection
        symbol_name: Symbol name to search for
        limit: Maximum number of results to return (default 100)
        role_filter: Optional role bitmask to filter by (e.g., ROLE_READ_ACCESS=8)
        exact: If True (default), match exact symbol name; if False, match substring (LIKE)

    Returns:
        List of dictionaries with keys:
            - symbol_name: Full SCIP symbol identifier
            - file_path: Relative file path
            - line: Line number (0-indexed)
            - column: Column number (0-indexed)
            - kind: Symbol kind (Class, Method, etc.)
            - role: Role bitmask
    """
    cursor = conn.cursor()

    # Build WHERE clause and parameter list
    where_clauses = [f"(o.role & {ROLE_DEFINITION}) = 0"]  # Exclude definitions

    # Add role filter if specified (parameterized to prevent SQL injection)
    params: List[Any] = []
    if role_filter is not None:
        where_clauses.append("(o.role & ?) != 0")
        params.append(role_filter)

    if exact:
        # Sanitize input for FTS5 (escape double quotes)
        safe_symbol_name = symbol_name.replace('"', '""')

        # Use FTS5 for fast symbol name lookup
        # FTS5 MATCH query returns matching symbol IDs instantly
        # Then join to occurrences using indexed symbol_id column
        fts_pattern = (
            f'"{safe_symbol_name}#" OR "{safe_symbol_name}()" OR "{safe_symbol_name}."'
        )

        where_clause = " AND ".join(where_clauses)

        query = f"""
            SELECT
                s.name as symbol_name,
                d.relative_path as file_path,
                o.start_line as line,
                o.start_char as column,
                s.kind as kind,
                o.role as role
            FROM symbols_fts fts
            JOIN symbols s ON fts.rowid = s.id
            JOIN occurrences o ON o.symbol_id = s.id
            JOIN documents d ON o.document_id = d.id
            WHERE fts.name MATCH ?
                AND {where_clause}
            ORDER BY d.relative_path, o.start_line
        """

        # Prepend FTS pattern to params
        params = [fts_pattern] + params

        # Conditionally add LIMIT clause (limit=0 means unlimited)
        if limit > 0:
            query += " LIMIT ?"
            params.append(limit)

        cursor.execute(query, tuple(params))
    else:
        # Fall back to LIKE for substring matching
        # FTS5 MATCH doesn't support true substring matching (requires token boundaries)
        # LIKE is slower but acceptable for fuzzy queries (not critical path)
        where_clause = " AND ".join(where_clauses)

        query = f"""
            SELECT
                s.name as symbol_name,
                d.relative_path as file_path,
                o.start_line as line,
                o.start_char as column,
                s.kind as kind,
                o.role as role
            FROM symbols s
            JOIN occurrences o ON o.symbol_id = s.id
            JOIN documents d ON o.document_id = d.id
            WHERE s.name LIKE ?
                AND {where_clause}
            ORDER BY d.relative_path, o.start_line
        """

        # Prepend LIKE pattern to params
        params = [f"%{symbol_name}%"] + params

        # Conditionally add LIMIT clause (limit=0 means unlimited)
        if limit > 0:
            query += " LIMIT ?"
            params.append(limit)

        cursor.execute(query, tuple(params))

    # Fetch results and convert to dictionaries
    results = []
    for row in cursor.fetchall():
        results.append(
            {
                "symbol_name": row[0],
                "file_path": row[1],
                "line": row[2],
                "column": row[3],
                "kind": row[4],
                "role": row[5],
            }
        )

    return results


def _get_dependencies_hybrid(
    conn: sqlite3.Connection,
    symbol_id: int,
    depth: int,
    scip_file: "Path",
) -> List[Dict[str, Any]]:
    """Find ALL symbols that the target symbol depends on using symbol_references table.

    Uses symbol_references table with SQL recursive CTE to find ALL dependencies
    regardless of scope (imports, fields, constructor parameters, method calls) in a single query.

    The scip_file parameter is kept for API compatibility but not used (all data is in database).
    """
    # Validate depth to prevent stack overflow
    if depth < 1 or depth > 10:
        raise ValueError(f"Depth must be between 1 and 10, got {depth}")

    cursor = conn.cursor()

    # SQL recursive CTE for transitive dependencies lookup
    # Replaces Python recursion with single database query (256x faster)
    query = """
        WITH target_and_nested AS (
            SELECT ? AS symbol_id
            UNION
            SELECT DISTINCT s_nested.id
            FROM symbols s_nested, symbols s_target
            WHERE s_target.id = ?
            AND s_nested.id != ?
            AND (
                -- If target ends with delimiter (# or .), match anything starting with target
                (s_target.name LIKE '%#' OR s_target.name LIKE '%.') AND s_nested.name LIKE s_target.name || '%'
                OR
                -- If target has no delimiter, require delimiter after target to prevent false positives
                (s_target.name NOT LIKE '%#' AND s_target.name NOT LIKE '%.')
                AND (s_nested.name LIKE s_target.name || '#%' OR s_nested.name LIKE s_target.name || '.%')
            )
        ),
        transitive_deps(symbol_id, depth, relationship_type) AS (
            -- Base case: direct dependencies (symbols that target references)
            SELECT DISTINCT sr.to_symbol_id, 1, sr.relationship_type
            FROM symbol_references sr
            JOIN target_and_nested tan ON sr.from_symbol_id = tan.symbol_id

            UNION

            -- Recursive case: transitive dependencies (symbols that dependencies reference)
            SELECT DISTINCT sr.to_symbol_id, td.depth + 1, sr.relationship_type
            FROM transitive_deps td
            JOIN symbol_references sr ON sr.from_symbol_id = td.symbol_id
            WHERE td.depth < ?
        )
        SELECT DISTINCT
            s.name as symbol_name,
            d.relative_path as file_path,
            o.start_line as line,
            o.start_char as column,
            s.kind as kind,
            td.depth,
            td.relationship_type as relationship
        FROM transitive_deps td
        JOIN symbols s ON td.symbol_id = s.id
        JOIN occurrences o ON o.symbol_id = s.id AND (o.role & ?) = ?
        JOIN documents d ON o.document_id = d.id
        WHERE (s.kind IS NULL OR s.kind NOT IN ('Local', 'Parameter'))
            AND s.name NOT LIKE 'local %'
        ORDER BY td.depth, s.name
    """
    cursor.execute(
        query,
        (symbol_id, symbol_id, symbol_id, depth, ROLE_DEFINITION, ROLE_DEFINITION),
    )

    results = []
    for row in cursor.fetchall():
        results.append(
            {
                "symbol_name": row[0],
                "file_path": row[1],
                "line": row[2],
                "column": row[3],
                "kind": row[4],
                "depth": row[5],
                "relationship": row[6],
            }
        )

    return results


def get_dependencies(
    conn: sqlite3.Connection,
    symbol_id: int,
    depth: int = 1,
    scip_file: Optional["Path"] = None,
) -> List[Dict[str, Any]]:
    """
    Get symbols that the target symbol depends on (outgoing references).

    HYBRID MODE (scip_file provided): Uses database-only occurrences table for ALL symbol references.
    LEGACY MODE (scip_file=None): Uses call_graph table for function calls only.

    Args:
        conn: SQLite database connection
        symbol_id: ID of the symbol to analyze
        depth: Depth of transitive dependencies (1 = direct only, 2+ = transitive)
        scip_file: Optional path to .scip file for hybrid mode (returns ALL references)

    Returns:
        List of dictionaries with keys:
            - symbol_name: Full SCIP symbol identifier
            - file_path: Relative file path
            - line: Line number (0-indexed)
            - column: Column number (0-indexed)
            - kind: Symbol kind (Class, Method, etc.)
            - relationship: Relationship type (call, reference, etc.)
    """
    # Use hybrid implementation if scip_file provided
    if scip_file is not None:
        return _get_dependencies_hybrid(conn, symbol_id, depth, scip_file)

    # Legacy call_graph implementation (function calls only)
    # Validate depth parameter
    if depth < 1 or depth > 10:
        raise ValueError(f"Depth must be between 1 and 10, got {depth}")

    cursor = conn.cursor()

    if depth == 1:
        # Direct dependencies only - simple JOIN on call_graph
        query = """
            SELECT DISTINCT
                s.name as symbol_name,
                d.relative_path as file_path,
                o.start_line as line,
                o.start_char as column,
                s.kind as kind,
                cg.relationship as relationship
            FROM call_graph cg
            JOIN symbols s ON cg.callee_symbol_id = s.id
            JOIN occurrences o ON o.symbol_id = s.id AND (o.role & 1) = 1
            JOIN documents d ON o.document_id = d.id
            WHERE cg.caller_symbol_id = ?
                AND (s.kind IS NULL OR s.kind NOT IN ('Local', 'Parameter'))
                AND s.name NOT LIKE 'local %'
            ORDER BY s.name
        """
        cursor.execute(query, (symbol_id,))
    else:
        # Transitive dependencies - recursive CTE
        query = """
            WITH RECURSIVE transitive_deps(symbol_id, depth, relationship) AS (
                -- Base case: direct dependencies
                SELECT cg.callee_symbol_id, 1, cg.relationship
                FROM call_graph cg
                WHERE cg.caller_symbol_id = ?

                UNION

                -- Recursive case: transitive dependencies
                SELECT cg.callee_symbol_id, td.depth + 1, cg.relationship
                FROM transitive_deps td
                JOIN call_graph cg ON td.symbol_id = cg.caller_symbol_id
                WHERE td.depth < ?
            )
            SELECT DISTINCT
                s.name as symbol_name,
                d.relative_path as file_path,
                o.start_line as line,
                o.start_char as column,
                s.kind as kind,
                td.relationship as relationship
            FROM transitive_deps td
            JOIN symbols s ON td.symbol_id = s.id
            JOIN occurrences o ON o.symbol_id = s.id AND (o.role & 1) = 1
            JOIN documents d ON o.document_id = d.id
            WHERE (s.kind IS NULL OR s.kind NOT IN ('Local', 'Parameter'))
                AND s.name NOT LIKE 'local %'
            ORDER BY s.name
        """
        cursor.execute(query, (symbol_id, depth))

    # Fetch results and convert to dictionaries
    results = []
    for row in cursor.fetchall():
        results.append(
            {
                "symbol_name": row[0],
                "file_path": row[1],
                "line": row[2],
                "column": row[3],
                "kind": row[4],
                "relationship": row[5],
            }
        )

    return results


def _get_dependents_hybrid(
    conn: sqlite3.Connection,
    symbol_id: int,
    depth: int,
    scip_file: "Path",
) -> List[Dict[str, Any]]:
    """Find ALL symbols that depend on the target symbol using symbol_references table.

    Uses symbol_references table (reverse direction) with SQL recursive CTE
    to find ALL dependents regardless of scope in a single query.
    """
    cursor = conn.cursor()

    # SQL recursive CTE for transitive dependents lookup
    # Replaces Python recursion with single database query (256x faster)
    query = """
        WITH target_and_nested AS (
            SELECT ? AS symbol_id
            UNION
            SELECT DISTINCT s_nested.id
            FROM symbols s_nested, symbols s_target
            WHERE s_target.id = ?
            AND s_nested.id != ?
            AND (
                -- If target ends with delimiter (# or .), match anything starting with target
                (s_target.name LIKE '%#' OR s_target.name LIKE '%.') AND s_nested.name LIKE s_target.name || '%'
                OR
                -- If target has no delimiter, require delimiter after target to prevent false positives
                (s_target.name NOT LIKE '%#' AND s_target.name NOT LIKE '%.')
                AND (s_nested.name LIKE s_target.name || '#%' OR s_nested.name LIKE s_target.name || '.%')
            )
        ),
        transitive_deps(symbol_id, depth, relationship_type) AS (
            -- Base case: direct dependents (symbols that reference target)
            SELECT DISTINCT sr.from_symbol_id, 1, sr.relationship_type
            FROM symbol_references sr
            JOIN target_and_nested tan ON sr.to_symbol_id = tan.symbol_id

            UNION

            -- Recursive case: transitive dependents (symbols that reference dependents)
            SELECT DISTINCT sr.from_symbol_id, td.depth + 1, sr.relationship_type
            FROM transitive_deps td
            JOIN symbol_references sr ON sr.to_symbol_id = td.symbol_id
            WHERE td.depth < ?
        )
        SELECT DISTINCT
            s.name as symbol_name,
            d.relative_path as file_path,
            o.start_line as line,
            o.start_char as column,
            s.kind as kind,
            td.depth,
            td.relationship_type as relationship
        FROM transitive_deps td
        JOIN symbols s ON td.symbol_id = s.id
        JOIN occurrences o ON o.symbol_id = s.id AND (o.role & ?) = ?
        JOIN documents d ON o.document_id = d.id
        WHERE (s.kind IS NULL OR s.kind NOT IN ('Local', 'Parameter'))
            AND s.name NOT LIKE 'local %'
        ORDER BY td.depth, s.name
    """
    cursor.execute(
        query,
        (symbol_id, symbol_id, symbol_id, depth, ROLE_DEFINITION, ROLE_DEFINITION),
    )

    results = []
    for row in cursor.fetchall():
        results.append(
            {
                "symbol_name": row[0],
                "file_path": row[1],
                "line": row[2],
                "column": row[3],
                "kind": row[4],
                "depth": row[5],
                "relationship": row[6],
            }
        )

    return results


def get_dependents(
    conn: sqlite3.Connection,
    symbol_id: int,
    depth: int = 1,
    scip_file: Optional["Path"] = None,
) -> List[Dict[str, Any]]:
    """
    Get symbols that depend on the target symbol (incoming references).

    HYBRID MODE (scip_file provided): Uses occurrences table + protobuf for ALL symbol references.
    LEGACY MODE (scip_file=None): Uses call_graph table for function calls only.

    Args:
        conn: SQLite database connection
        symbol_id: ID of the symbol to analyze
        depth: Depth of transitive dependents (1 = direct only, 2+ = transitive)
        scip_file: Optional path to .scip file for hybrid mode (returns ALL references)

    Returns:
        List of dictionaries with keys:
            - symbol_name: Full SCIP symbol identifier
            - file_path: Relative file path
            - line: Line number (0-indexed)
            - column: Column number (0-indexed)
            - kind: Symbol kind (Class, Method, etc.)
            - relationship: Relationship type (call, reference, etc.)
    """
    # Use hybrid implementation if scip_file provided
    if scip_file is not None:
        return _get_dependents_hybrid(conn, symbol_id, depth, scip_file)

    # Legacy call_graph implementation (function calls only)
    # Validate depth parameter
    if depth < 1 or depth > 10:
        raise ValueError(f"Depth must be between 1 and 10, got {depth}")

    cursor = conn.cursor()

    if depth == 1:
        # Direct dependents only - simple JOIN on call_graph (reversed direction)
        query = """
            SELECT DISTINCT
                s.name as symbol_name,
                d.relative_path as file_path,
                o.start_line as line,
                o.start_char as column,
                s.kind as kind,
                1 as depth,
                cg.relationship as relationship
            FROM call_graph cg
            JOIN symbols s ON cg.caller_symbol_id = s.id
            JOIN occurrences o ON o.symbol_id = s.id AND (o.role & 1) = 1
            JOIN documents d ON o.document_id = d.id
            WHERE cg.callee_symbol_id = ?
                AND (s.kind IS NULL OR s.kind NOT IN ('Local', 'Parameter'))
                AND s.name NOT LIKE 'local %'
            ORDER BY s.name
        """
        cursor.execute(query, (symbol_id,))
    else:
        # Transitive dependents - recursive CTE (reversed direction)
        query = """
            WITH RECURSIVE transitive_deps(symbol_id, depth, relationship) AS (
                -- Base case: direct dependents
                SELECT cg.caller_symbol_id, 1, cg.relationship
                FROM call_graph cg
                WHERE cg.callee_symbol_id = ?

                UNION

                -- Recursive case: transitive dependents
                SELECT cg.caller_symbol_id, td.depth + 1, cg.relationship
                FROM transitive_deps td
                JOIN call_graph cg ON td.symbol_id = cg.callee_symbol_id
                WHERE td.depth < ?
            )
            SELECT DISTINCT
                s.name as symbol_name,
                d.relative_path as file_path,
                o.start_line as line,
                o.start_char as column,
                s.kind as kind,
                td.depth as depth,
                td.relationship as relationship
            FROM transitive_deps td
            JOIN symbols s ON td.symbol_id = s.id
            JOIN occurrences o ON o.symbol_id = s.id AND (o.role & 1) = 1
            JOIN documents d ON o.document_id = d.id
            WHERE (s.kind IS NULL OR s.kind NOT IN ('Local', 'Parameter'))
                AND s.name NOT LIKE 'local %'
            ORDER BY s.name
        """
        cursor.execute(query, (symbol_id, depth))

    # Fetch results and convert to dictionaries
    results = []
    for row in cursor.fetchall():
        results.append(
            {
                "symbol_name": row[0],
                "file_path": row[1],
                "line": row[2],
                "column": row[3],
                "kind": row[4],
                "depth": row[5],
                "relationship": row[6],
            }
        )

    return results


def analyze_impact(
    conn: sqlite3.Connection,
    symbol_id: int,
    depth: int = 3,
    scip_file: Optional["Path"] = None,
) -> List[Dict[str, Any]]:
    """
    Analyze impact of changing symbol.

    HYBRID MODE (scip_file provided): Uses get_dependents() hybrid mode for ALL symbol references.
    LEGACY MODE (scip_file=None): Uses call_graph table for function calls only.

    Returns all symbols transitively dependent on target symbol,
    grouped by file path with counts.

    Args:
        conn: SQLite database connection
        symbol_id: Target symbol ID
        depth: Maximum dependency depth (1-10)
        scip_file: Optional path to .scip file for hybrid mode (returns ALL references)

    Returns:
        List of dicts with keys:
            - file_path: Relative file path
            - symbol_count: Number of impacted symbols in file
            - symbols: List of impacted symbol names
    """
    if depth < 1 or depth > 10:
        raise ValueError(f"Depth must be between 1 and 10, got {depth}")

    # Get transitive dependents using hybrid or legacy mode
    dependents = get_dependents(conn, symbol_id, depth=depth, scip_file=scip_file)

    # Group by file_path
    file_map: Dict[str, List[str]] = {}
    for dep in dependents:
        file_path = dep["file_path"]
        symbol_name = dep["symbol_name"]
        if file_path not in file_map:
            file_map[file_path] = []
        file_map[file_path].append(symbol_name)

    # Convert to list of dicts with deduplication
    results = []
    for file_path, symbols in file_map.items():
        # Deduplicate symbols while preserving order
        seen: set = set()
        unique_symbols = []
        for s in symbols:
            if s not in seen:
                seen.add(s)
                unique_symbols.append(s)
        results.append(
            {
                "file_path": file_path,
                "symbol_count": len(unique_symbols),
                "symbols": sorted(unique_symbols),
            }
        )

    # Sort by symbol_count DESC
    return sorted(results, key=lambda r: r["symbol_count"], reverse=True)
