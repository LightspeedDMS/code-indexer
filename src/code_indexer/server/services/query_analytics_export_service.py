"""QueryAnalyticsExportService -- export search event log to Excel (Issue #1160).

Provides dual-backend storage (SQLite/Postgres) for export history tracking,
and an export service that queries the search_event_log, writes an Excel file,
and records the result in the query_analytics_exports table.

Follows the SearchEventLogWriter pattern for dual-backend design.
"""

import logging
import os
import re
import sqlite3
import time
import uuid as _uuid_module
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Tuple, cast

from code_indexer.server.services.xray_evaluator_sources import split_hashes

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Excel column names in exact canonical order (spec)
_EXCEL_COLUMNS = [
    "timestamp",
    "user",
    "repo_alias",
    "search_type",
    "query_text",
    # Bug #2114: true when query_text was cut to its stored cap; blank for
    # rows recorded before the column existed.
    "query_text_truncated",
    "voyage_cache_hit",
    "voyage_cache_mode",
    "voyage_latency_ms",
    "cohere_cache_hit",
    "cohere_cache_mode",
    "cohere_latency_ms",
    "total_latency_ms",
    "result_count",
    "node_id",
    "correlation_id",
    # Story #2117: blank for rows recorded before the columns existed.
    "tool",
    "door",
    "outcome",
    "error_class",
    # Story #2203 (X-Ray): the evaluator hash(es) and stored pattern name(s),
    # and the source looked up by hash, cut to the XLSX cell limit; the flag
    # is true when the cell is not the whole source. Blank for other rows.
    "evaluator_sha256",
    "xray_pattern_name",
    "evaluator_source",
    "evaluator_source_truncated",
]

# Rows per store read (Bug #2106). An export reads every matching row in
# batches of this size and streams them into the workbook, so memory stays
# bounded by one batch however many rows match.
_EXPORT_BATCH_SIZE = 5_000

# A worksheet holds at most 1,048,576 rows; one is the header. Beyond this an
# export continues on a new worksheet (with its own header) -- never truncated.
_EXCEL_MAX_DATA_ROWS_PER_SHEET = 1_048_575

# Number of seconds in one day
_SECONDS_PER_DAY = 86_400

# SQLite connection timeout
_SQLITE_TIMEOUT_SECONDS = 30

# Allowed fields for update_export (defence against SQL injection via dynamic SET)
_UPDATE_ALLOWED = frozenset(
    {
        "status",
        "file_path",
        "file_size_bytes",
        "row_count",
        "error_message",
        "retention_until",
    }
)

# Staleness threshold for orphan reconciliation (Bug #1228).
# Exports still in pending/running after this many seconds are considered orphaned.
# Exports are no longer capped (Bug #2106), so a very large one can run longer
# than this; reconciliation runs only at server startup, and a running export a
# peer's startup marked failed is overwritten by its own final status update.
# 300 seconds (5 min) still definitively catches hours-old stuck exports from
# node deaths.
_DEFAULT_ORPHAN_THRESHOLD_SECONDS = 300.0

# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _validate_export_id(export_id: str) -> None:
    """Raise ValueError if export_id is not a valid UUID string.

    Using uuid.UUID() is the canonical way to validate UUID format and prevents
    path traversal via export_id (e.g. '../../etc/passwd').
    """
    try:
        parsed = _uuid_module.UUID(export_id)
        if str(parsed) != export_id.lower():
            raise ValueError("non-canonical UUID form")
    except (ValueError, AttributeError) as exc:
        raise ValueError(
            f"export_id must be a canonical UUID string, got {export_id!r}"
        ) from exc


class _NeutralExportError(RuntimeError):
    # ^ security message written by this module only; never stored text
    pass


class _SearchEventReadFailure(Exception):
    # ^ invariant __cause__ = the database error of a search event store read
    pass


def _export_failure(exc: Exception) -> Tuple[str, BaseException]:
    # ^ invariant returns (history and log text, exception to raise)
    # ^ why a read keeps the database message, not neutral; locked != missing table (#2105)
    if isinstance(exc, _SearchEventReadFailure) and exc.__cause__ is not None:
        return str(exc.__cause__), exc.__cause__
    if isinstance(exc, _NeutralExportError):
        return str(exc), exc
    # ^ security str(exc) may echo a cell (openpyxl IllegalCharacterError) => never stored, logged or raised
    neutral = _NeutralExportError(f"export failed ({type(exc).__name__})")
    neutral.__cause__ = exc
    return str(neutral), neutral


def _build_record_tuple(record: Dict[str, Any]) -> tuple:
    """Map a record dict to the canonical INSERT column order."""
    return (
        record["id"],
        record["initiated_by"],
        record["created_at"],
        record["status"],
        record["filter_summary"],
        record.get("file_path"),
        record.get("file_size_bytes"),
        record.get("row_count"),
        record.get("error_message"),
        record.get("retention_until"),
    )


def _bounded_by_start(filters: Dict[str, Any], start_ts: float) -> Dict[str, Any]:
    """Return filters whose to_timestamp is at most the export start time.

    Bug #2106: an export covers the rows recorded before it started. This
    bounds the matching set so the batched read terminates even while new
    searches keep being recorded.
    """
    bounded = dict(filters)
    to_ts = bounded.get("to_timestamp")
    bounded["to_timestamp"] = start_ts if to_ts is None else min(to_ts, start_ts)
    return bounded


# ^ external an XLSX cell holds <= 32767 UTF-16 code units (Excel specification)
XLSX_CELL_MAX_UTF16 = 32_767
# ^ external XML 1.0 refuses these code points; lxml raises "All strings must be XML compatible"
_XML_ILLEGAL = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\ud800-\udfff￾￿]")
_REPLACEMENT_CHARACTER = "�"
# Columns whose text `_with_evaluator_sources` already made cell-safe.
_CELL_SAFE_COLUMNS = frozenset({"evaluator_source"})


def _cut_utf16(text: str, limit: int) -> str:
    # ^ invariant result <= `limit` UTF-16 code units; never ends inside a surrogate pair
    if len(text) * 2 <= limit:
        return text
    units = 0
    for index, character in enumerate(text):
        units += 2 if ord(character) > 0xFFFF else 1
        if units > limit:
            return text[:index]
    return text


def _sheet_text(value: str) -> Tuple[str, bool]:
    # ^ invariant returns (cell-safe text, whether it differs from `value`)
    cleaned = _XML_ILLEGAL.sub(_REPLACEMENT_CHARACTER, value)
    cell = _cut_utf16(cleaned, XLSX_CELL_MAX_UTF16)
    return cell, cell != value


def _literal_cell(ws: Any, text: str) -> Any:
    from openpyxl.cell import WriteOnlyCell

    cell = WriteOnlyCell(ws, value=text)
    # ^ security data_type "s" after the value; inferred type makes "=..." a formula (<f>)
    cell.data_type = "s"
    return cell


def _sheet_row(ws: Any, row: Dict[str, Any], keys: List[str]) -> List[Any]:
    values: List[Any] = []
    for key in keys:
        value = row.get(key)
        if isinstance(value, str):
            text = value if key in _CELL_SAFE_COLUMNS else _sheet_text(value)[0]
            values.append(_literal_cell(ws, text))
        else:
            values.append(value)
    return values


# Workbook/worksheet parameters below are typed Any: openpyxl is imported
# lazily and ships no type stubs.
def _new_sheet(wb: Any, number: int) -> Any:
    ws = wb.create_sheet(title=f"Searches {number}")
    ws.append(_EXCEL_COLUMNS)
    return ws


def _append_rows(wb: Any, rows: Iterable[Dict[str, Any]]) -> int:
    """Append rows to write-only workbook sheets; return the number written.

    Each sheet takes at most _EXCEL_MAX_DATA_ROWS_PER_SHEET data rows; the
    first sheet exists even when there are no rows (header-only export).
    "user" in Excel maps to "username" in the store.
    """
    keys = ["username" if c == "user" else c for c in _EXCEL_COLUMNS]
    sheet_number = 1
    ws = _new_sheet(wb, sheet_number)
    in_sheet = 0
    written = 0
    for row in rows:
        if in_sheet == _EXCEL_MAX_DATA_ROWS_PER_SHEET:
            sheet_number += 1
            ws = _new_sheet(wb, sheet_number)
            in_sheet = 0
        ws.append(_sheet_row(ws, row, keys))
        in_sheet += 1
        written += 1
    return written


_SourceCells = Dict[str, Tuple[str, bool]]


def _lookup_batch_sources(
    search_backend: Any, batch: List[Dict[str, Any]]
) -> _SourceCells:
    hashes = {sha for row in batch for sha in split_hashes(row.get("evaluator_sha256"))}
    # ^ perf one cell + 1 characters per source read; > the cell => cut anyway
    if not hashes:
        return {}
    return dict(
        search_backend.evaluator_sources(hashes, max_chars=XLSX_CELL_MAX_UTF16 + 1)
    )


def _with_evaluator_sources(
    batch: List[Dict[str, Any]], found: _SourceCells
) -> List[Dict[str, Any]]:
    # ^ perf one cell per distinct hash list per batch; sanitised once
    cells: Dict[Optional[str], Tuple[Optional[str], Optional[bool]]] = {}
    for row in batch:
        key = row.get("evaluator_sha256")
        if key not in cells:
            cells[key] = _evaluator_cell(key, found)
        row["evaluator_source"], row["evaluator_source_truncated"] = cells[key]
    return batch


def _evaluator_cell(
    column_sha256: Optional[str], found: _SourceCells
) -> Tuple[Optional[str], Optional[bool]]:
    # ^ invariant flag = source gone, stored truncated or escaped, or text altered or cut
    hashes = split_hashes(column_sha256)
    if not hashes:
        return None, None
    parts: List[str] = []
    partial = False
    for sha in hashes:
        if sha not in found:
            partial = True
            continue
        source, truncated = found[sha]
        partial = partial or truncated
        parts.append(source if len(hashes) == 1 else f"// sha256:{sha}\n{source}")
    if not parts:
        return None, True
    cell, altered = _sheet_text("\n\n".join(parts))
    return cell, partial or altered


def _discard_workbook(wb: Any, file_path: Path) -> None:
    """Release an abandoned write-only workbook's temp files, leaving no file.

    openpyxl removes a write-only sheet's temp file only when the workbook is
    saved (otherwise at process exit), so save it and delete the result.
    """
    try:
        wb.save(str(file_path))
    except Exception as exc:
        # ^ security type only; str(exc) may echo a cell
        logger.warning(
            "QueryAnalyticsExportService: could not release temp files of the "
            "abandoned export workbook %s: %s",
            file_path,
            type(exc).__name__,
        )
    finally:
        _remove_export_file(file_path)


def _remove_export_file(file_path: Path) -> None:
    # ^ security an OSError names the path => logged as its type only, never raised over the export's own error
    try:
        file_path.unlink(missing_ok=True)
    except OSError as exc:
        logger.warning(
            "QueryAnalyticsExportService: could not remove a failed export file: %s",
            type(exc).__name__,
        )


def _validate_update_fields(fields: Dict[str, Any]) -> None:
    for key in fields:
        if key not in _UPDATE_ALLOWED:
            raise ValueError(f"update_export: unknown field {key!r}")


def _delete_expired_rows(rows: list, conn: Any, placeholder: str) -> int:
    """Remove files and DELETE DB rows for each expired export. Returns count."""
    count = 0
    for row in rows:
        file_path = row["file_path"] if hasattr(row, "keys") else row[1]
        if file_path:
            try:
                os.remove(file_path)
            except FileNotFoundError:
                pass
            except Exception as exc:
                logger.warning(
                    "evict_old_exports: could not remove %s: %s", file_path, exc
                )
        row_id = row["id"] if hasattr(row, "keys") else row[0]
        conn.execute(
            f"DELETE FROM query_analytics_exports WHERE id = {placeholder}",
            (row_id,),
        )
        count += 1
    return count


# ---------------------------------------------------------------------------
# Table DDL (shared, parameterised on timestamp type)
# ---------------------------------------------------------------------------

_CREATE_TABLE_SQLITE = """
    CREATE TABLE IF NOT EXISTS query_analytics_exports (
        id              TEXT PRIMARY KEY,
        initiated_by    TEXT NOT NULL,
        created_at      REAL NOT NULL,
        status          TEXT NOT NULL,
        filter_summary  TEXT NOT NULL,
        file_path       TEXT,
        file_size_bytes INTEGER,
        row_count       INTEGER,
        error_message   TEXT,
        retention_until REAL
    )
"""

_CREATE_TABLE_POSTGRES = """
    CREATE TABLE IF NOT EXISTS query_analytics_exports (
        id              TEXT PRIMARY KEY,
        initiated_by    TEXT NOT NULL,
        created_at      DOUBLE PRECISION NOT NULL,
        status          TEXT NOT NULL,
        filter_summary  TEXT NOT NULL,
        file_path       TEXT,
        file_size_bytes INTEGER,
        row_count       INTEGER,
        error_message   TEXT,
        retention_until DOUBLE PRECISION
    )
"""

_INSERT_SQL_SQLITE = """
    INSERT INTO query_analytics_exports
        (id, initiated_by, created_at, status, filter_summary,
         file_path, file_size_bytes, row_count, error_message, retention_until)
    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
"""

_INSERT_SQL_POSTGRES = """
    INSERT INTO query_analytics_exports
        (id, initiated_by, created_at, status, filter_summary,
         file_path, file_size_bytes, row_count, error_message, retention_until)
    VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""

# ---------------------------------------------------------------------------
# SQLite backend
# ---------------------------------------------------------------------------


class QueryAnalyticsExportSqliteBackend:
    """SQLite backend for query_analytics_exports table (solo/dev mode)."""

    def __init__(self, db_path: str) -> None:
        self._db_path = db_path
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        try:
            conn = sqlite3.connect(self._db_path, timeout=_SQLITE_TIMEOUT_SECONDS)
            try:
                conn.execute("PRAGMA journal_mode = WAL")
                conn.execute(_CREATE_TABLE_SQLITE)
                conn.commit()
            finally:
                conn.close()
        except Exception as exc:
            logger.warning(
                "QueryAnalyticsExportSqliteBackend: schema setup failed: %s", exc
            )

    def _conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self._db_path, timeout=_SQLITE_TIMEOUT_SECONDS)
        conn.row_factory = sqlite3.Row
        return conn

    def create_export(self, record: Dict[str, Any]) -> None:
        conn = self._conn()
        try:
            conn.execute(_INSERT_SQL_SQLITE, _build_record_tuple(record))
            conn.commit()
        finally:
            conn.close()

    def update_export(self, export_id: str, **fields: Any) -> None:
        if not fields:
            return
        _validate_update_fields(fields)
        set_clauses = [f"{k} = ?" for k in fields]
        params: List[Any] = list(fields.values()) + [export_id]
        conn = self._conn()
        try:
            conn.execute(
                f"UPDATE query_analytics_exports SET {', '.join(set_clauses)} WHERE id = ?",
                params,
            )
            conn.commit()
        finally:
            conn.close()

    def list_exports(self, export_id: Optional[str] = None) -> List[Dict[str, Any]]:
        conn = self._conn()
        try:
            if export_id is not None:
                rows = conn.execute(
                    "SELECT * FROM query_analytics_exports WHERE id = ? "
                    "ORDER BY created_at DESC",
                    (export_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM query_analytics_exports ORDER BY created_at DESC"
                ).fetchall()
            return [dict(row) for row in rows]
        finally:
            conn.close()

    def evict_old_exports(self, now_ts: float) -> int:
        """Delete expired exports (retention_until < now_ts) and their files."""
        conn = self._conn()
        try:
            rows = conn.execute(
                "SELECT id, file_path FROM query_analytics_exports "
                "WHERE retention_until IS NOT NULL AND retention_until < ?",
                (now_ts,),
            ).fetchall()
            count = _delete_expired_rows(rows, conn, "?")
            conn.commit()
            return count
        finally:
            conn.close()

    def reconcile_orphaned_exports(
        self,
        threshold_seconds: float = _DEFAULT_ORPHAN_THRESHOLD_SECONDS,
        error: str = "interrupted by worker restart",
    ) -> int:
        """Mark stale pending/running exports as failed (Bug #1228).

        Cluster-safe predicate: only affects exports whose created_at is older
        than threshold_seconds ago.  A legitimately-running export on another
        cluster node (started seconds/minutes ago) is NOT within the orphan
        window.  Exports that have been stuck for hours (e.g. after an NFS
        outage that killed the owning worker) are definitively caught.

        Returns the number of rows transitioned to 'failed'.
        """
        cutoff_ts = time.time() - threshold_seconds
        conn = self._conn()
        try:
            cur = conn.execute(
                "UPDATE query_analytics_exports "
                "SET status = 'failed', error_message = ? "
                "WHERE status IN ('pending', 'running') AND created_at < ?",
                (error, cutoff_ts),
            )
            conn.commit()
            return cur.rowcount
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# PostgreSQL backend
# ---------------------------------------------------------------------------


class QueryAnalyticsExportPostgresBackend:
    """PostgreSQL backend for query_analytics_exports table (cluster mode)."""

    def __init__(self, pool: Any) -> None:
        self._pool = pool
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        try:
            with self._pool.connection() as conn:
                conn.execute(_CREATE_TABLE_POSTGRES)
                conn.commit()
        except Exception as exc:
            logger.warning(
                "QueryAnalyticsExportPostgresBackend: schema setup failed: %s", exc
            )

    def create_export(self, record: Dict[str, Any]) -> None:
        with self._pool.connection() as conn:
            conn.execute(_INSERT_SQL_POSTGRES, _build_record_tuple(record))
            conn.commit()

    def update_export(self, export_id: str, **fields: Any) -> None:
        if not fields:
            return
        _validate_update_fields(fields)
        set_clauses = [f"{k} = %s" for k in fields]
        params: List[Any] = list(fields.values()) + [export_id]
        with self._pool.connection() as conn:
            conn.execute(
                f"UPDATE query_analytics_exports SET {', '.join(set_clauses)} WHERE id = %s",
                params,
            )
            conn.commit()

    def list_exports(self, export_id: Optional[str] = None) -> List[Dict[str, Any]]:
        with self._pool.connection() as conn:
            if export_id is not None:
                rows = conn.execute(
                    "SELECT * FROM query_analytics_exports WHERE id = %s "
                    "ORDER BY created_at DESC",
                    (export_id,),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM query_analytics_exports ORDER BY created_at DESC"
                ).fetchall()
            return [
                {
                    "id": row[0],
                    "initiated_by": row[1],
                    "created_at": row[2],
                    "status": row[3],
                    "filter_summary": row[4],
                    "file_path": row[5],
                    "file_size_bytes": row[6],
                    "row_count": row[7],
                    "error_message": row[8],
                    "retention_until": row[9],
                }
                for row in rows
            ]

    def evict_old_exports(self, now_ts: float) -> int:
        with self._pool.connection() as conn:
            rows = conn.execute(
                "SELECT id, file_path FROM query_analytics_exports "
                "WHERE retention_until IS NOT NULL AND retention_until < %s",
                (now_ts,),
            ).fetchall()
            count = _delete_expired_rows(rows, conn, "%s")
            conn.commit()
            return count

    def reconcile_orphaned_exports(
        self,
        threshold_seconds: float = _DEFAULT_ORPHAN_THRESHOLD_SECONDS,
        error: str = "interrupted by worker restart",
    ) -> int:
        """Mark stale pending/running exports as failed (Bug #1228).

        Cluster-safe: only affects exports whose created_at is older than
        threshold_seconds ago, preserving legitimately-running exports on
        other cluster nodes.

        Returns the number of rows transitioned to 'failed'.
        """
        cutoff_ts = time.time() - threshold_seconds
        with self._pool.connection() as conn:
            cur = conn.execute(
                "UPDATE query_analytics_exports "
                "SET status = 'failed', error_message = %s "
                "WHERE status IN ('pending', 'running') AND created_at < %s",
                (error, cutoff_ts),
            )
            conn.commit()
            return int(cur.rowcount)


# ---------------------------------------------------------------------------
# Export service
# ---------------------------------------------------------------------------


class QueryAnalyticsExportService:
    """Service for exporting search event log data to Excel files.

    Manages export lifecycle: create pending record, fetch+write Excel,
    update completed/failed, and evict old files.
    """

    def __init__(self, backend: Any, golden_repos_dir: str) -> None:
        self._backend = backend
        self._golden_repos_dir = golden_repos_dir

    def export_path(self, export_id: str) -> Path:
        """Compute the export file path for a given export_id.

        Raises ValueError if export_id is not a valid UUID (prevents path traversal).
        """
        _validate_export_id(export_id)
        return (
            Path(self._golden_repos_dir)
            / "cidx-exports"
            / "query-analytics"
            / f"{export_id}.xlsx"
        )

    def ensure_export_dir(self) -> None:
        """Create the export directory if it does not exist."""
        export_dir = Path(self._golden_repos_dir) / "cidx-exports" / "query-analytics"
        export_dir.mkdir(parents=True, exist_ok=True)

    def build_filter_summary(self, filters: Dict[str, Any]) -> str:
        """Build a human-readable summary string from the applied filters."""
        parts = []

        if filters.get("user"):
            parts.append(f"user={filters['user']}")
        if filters.get("repo_alias"):
            parts.append(f"repo_alias={filters['repo_alias']}")
        if filters.get("search_type") and filters["search_type"] != "all":
            parts.append(f"search_type={filters['search_type']}")
        if filters.get("cache_hit_filter") and filters["cache_hit_filter"] != "all":
            parts.append(f"cache_hit={filters['cache_hit_filter']}")
        # Story #2117 dimensions.
        for name in ("door", "outcome", "tool", "correlation_id"):
            if filters.get(name):
                parts.append(f"{name}={filters[name]}")

        from_ts = filters.get("from_timestamp")
        to_ts = filters.get("to_timestamp")
        if from_ts is not None:
            dt = datetime.fromtimestamp(from_ts, tz=timezone.utc)
            parts.append(f"from={dt.strftime('%Y-%m-%d')}")
        if to_ts is not None:
            dt = datetime.fromtimestamp(to_ts, tz=timezone.utc)
            parts.append(f"to={dt.strftime('%Y-%m-%d')}")

        if not parts:
            return "All searches"
        return ", ".join(parts)

    def _iter_export_rows(
        self, search_backend: Any, filters: Dict[str, Any]
    ) -> Iterator[Dict[str, Any]]:
        """Yield every row matching `filters`, oldest first (Bug #2106).

        Reads bounded batches of _EXPORT_BATCH_SIZE rows with keyset paging on
        (timestamp, id); every filter, including cache_hit_filter, is applied
        by the store before its LIMIT. Read failures propagate (Bug #2105).

        Termination: run_export bounds `to_timestamp` by the export start time,
        so the matching set is finite; each full batch strictly advances the
        keyset (checked below), and the first short batch ends the loop.
        """
        after: Optional[Tuple[float, int]] = None
        while True:
            try:
                batch = search_backend.query_for_export(
                    filters, limit=_EXPORT_BATCH_SIZE, after=after
                )
                found = _lookup_batch_sources(search_backend, batch)
            except Exception as exc:
                raise _SearchEventReadFailure() from exc
            yield from _with_evaluator_sources(batch, found)
            if len(batch) < _EXPORT_BATCH_SIZE:
                return
            last = batch[-1]
            keyset = (last["timestamp"], last["id"])
            if after is not None and keyset <= after:
                raise RuntimeError(
                    f"export read did not advance past {after!r} (got {keyset!r})"
                )
            after = keyset

    def run_export(
        self,
        export_id: str,
        filters: Dict[str, Any],
        initiated_by: str,
        export_retention_days: int,
        search_event_log_backend: Any,
    ) -> None:
        """Execute the export: fetch rows, write Excel, update DB record.

        Creates the DB record as 'pending', writes Excel, then updates to
        'completed' or 'failed'. Intended to be called by the BGM worker thread.

        Bug #2105: a failure (including a failed store read) is recorded as
        'failed' with its reason in the export history AND re-raised, so the
        background job fails too instead of reporting success.
        """
        now = time.time()
        filter_summary = self.build_filter_summary(filters)
        record: Dict[str, Any] = {
            "id": export_id,
            "initiated_by": initiated_by,
            "created_at": now,
            "status": "pending",
            "filter_summary": filter_summary,
        }
        self._backend.create_export(record)

        file_path: Optional[Path] = None
        try:
            if search_event_log_backend is None:
                raise _NeutralExportError("search event log is not initialized")
            self.ensure_export_dir()
            file_path = self.export_path(export_id)
            rows = self._iter_export_rows(
                search_event_log_backend, _bounded_by_start(filters, now)
            )
            row_count = self._write_excel(rows, file_path)

            file_size = file_path.stat().st_size
            retention_until = now + export_retention_days * _SECONDS_PER_DAY
            self._backend.update_export(
                export_id,
                status="completed",
                file_path=str(file_path),
                file_size_bytes=file_size,
                row_count=row_count,
                retention_until=retention_until,
                # A peer's startup reconcile may have marked this running
                # export failed with a reason; completion supersedes it.
                error_message=None,
            )
        except Exception as exc:
            failure, raised = _export_failure(exc)
            logger.error(
                "QueryAnalyticsExportService: export %s failed: %s", export_id, failure
            )
            if file_path is not None:
                _remove_export_file(file_path)
            error_retention_until = now + export_retention_days * _SECONDS_PER_DAY
            self._backend.update_export(
                export_id,
                status="failed",
                error_message=failure,
                retention_until=error_retention_until,
            )
            # ^ security BackgroundJobManager stores and logs str(raised); never a cell's text
            raise raised

    def _write_excel(self, rows: Iterable[Dict[str, Any]], file_path: Path) -> int:
        """Stream rows into an Excel file at file_path; return rows written.

        Bug #2106: a write-only workbook keeps memory bounded however many rows
        are streamed. Each worksheet holds at most _EXCEL_MAX_DATA_ROWS_PER_SHEET
        data rows under the canonical header; further rows continue on a new
        worksheet, so nothing is truncated.
        """
        import openpyxl

        wb = openpyxl.Workbook(write_only=True)
        try:
            written = _append_rows(wb, rows)
        except BaseException:
            _discard_workbook(wb, file_path)
            raise
        wb.save(str(file_path))
        return written

    def list_exports(self, export_id: Optional[str] = None) -> List[Dict[str, Any]]:
        """Return export records, adding a computed 'download_link' field.

        For completed exports the link is the REST download endpoint.
        For all other statuses (pending, running, failed) the link is None.
        """
        records = self._backend.list_exports(export_id=export_id)
        for record in records:
            if record.get("status") == "completed":
                record["download_link"] = (
                    f"/api/admin/search-events/exports/{record['id']}/download"
                )
            else:
                record["download_link"] = None
        return cast(List[Dict[str, Any]], records)

    def evict_old_exports(self, now_ts: float) -> int:
        """Delegate eviction to the backend."""
        return cast(int, self._backend.evict_old_exports(now_ts=now_ts))

    def reconcile_orphaned_exports(
        self,
        threshold_seconds: float = _DEFAULT_ORPHAN_THRESHOLD_SECONDS,
        error: str = "interrupted by worker restart",
    ) -> int:
        """Reconcile orphaned exports: transition stale pending/running rows to failed.

        Delegates to the backend.  Cluster-safe: only affects exports whose
        created_at is older than threshold_seconds ago (default 300 s / 5 min).

        Called on server startup to clear exports left stuck by worker death,
        server restart, or infrastructure outage (Bug #1228).

        Returns the number of rows transitioned to 'failed'.
        """
        return cast(
            int,
            self._backend.reconcile_orphaned_exports(
                threshold_seconds=threshold_seconds,
                error=error,
            ),
        )
