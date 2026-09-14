"""Story #1787 AC16 (amendment): cluster-shared self-calibration of the
per-language memory multiplier K.

Records `(language, source_bytes, decls, call_sites, candidate_edges,
actual_peak_rss)` per graph build and refines K over time. This state
MUST live in the shared database (SQLite solo, PostgreSQL cluster) --
builds route to arbitrary nodes via HAProxy, so a node-local K means
every node learns the same lesson independently and a repo that OOM'd on
node A still OOMs on node B (an earlier draft of this criterion assumed
node-local was acceptable; that reasoning was wrong for this deployment).

`get_k()` returns the MAX observed `actual_peak_rss / source_bytes`
multiplier for a language, never the average -- conservative by design,
matching `k_seed_table.CONSERVATIVE_DEFAULT_K`'s own "worst case wins"
seeding. A missing/no-sample language returns `None`; the CALLER (see
`k_seed_table.k_for_language` and the future TTL-cached read wrapper)
is responsible for degrading to the conservative default in that case --
never a wrong answer.
"""

from __future__ import annotations

import logging
import math
import os
import sqlite3
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Protocol

from code_indexer.server.storage.database_manager import DatabaseConnectionManager

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class KCalibrationSample:
    """One graph build's real measurements, as AC16 names them
    verbatim: "(language, source_bytes, decls, call_sites,
    candidate_edges, actual_peak_rss)".
    """

    language: str
    source_bytes: int
    decls: int
    call_sites: int
    candidate_edges: int
    actual_peak_rss: int


def _require_valid_sample(sample: KCalibrationSample) -> None:
    if not sample.language:
        raise ValueError("KCalibrationSample.language must be a non-empty string")
    if sample.source_bytes <= 0:
        raise ValueError(
            f"KCalibrationSample.source_bytes must be positive, got {sample.source_bytes}"
        )
    for field_name, value in (
        ("decls", sample.decls),
        ("call_sites", sample.call_sites),
        ("candidate_edges", sample.candidate_edges),
        ("actual_peak_rss", sample.actual_peak_rss),
    ):
        if value < 0:
            raise ValueError(
                f"KCalibrationSample.{field_name} must be non-negative, got {value}"
            )


class SqliteKCalibrationBackend:
    """SQLite backend for K calibration samples (solo deployment).

    Uses a dedicated DB file via `DatabaseConnectionManager` (the same
    connection-management primitive `QueryEmbeddingCacheSqliteBackend`
    already uses), so calibration writes never contend with main server
    state.
    """

    def __init__(self, db_path: str) -> None:
        if not db_path:
            raise ValueError("db_path must be a non-empty string")
        os.makedirs(os.path.dirname(os.path.abspath(db_path)), exist_ok=True)
        self._db_path = db_path
        self._conn_manager = DatabaseConnectionManager.get_instance(db_path)

        conn = self._conn_manager.get_connection()
        conn.execute("PRAGMA journal_mode=WAL")

        self._ensure_schema()

    def _ensure_schema(self) -> None:
        def operation(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS xray_graph_k_calibration_samples (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    language         TEXT    NOT NULL,
                    source_bytes     INTEGER NOT NULL,
                    decls            INTEGER NOT NULL,
                    call_sites       INTEGER NOT NULL,
                    candidate_edges  INTEGER NOT NULL,
                    actual_peak_rss  INTEGER NOT NULL,
                    recorded_at      REAL    NOT NULL
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS idx_xray_k_calibration_language
                ON xray_graph_k_calibration_samples (language)
                """
            )

        self._conn_manager.execute_atomic(operation)

    def record_sample(self, sample: KCalibrationSample) -> None:
        """Persists one build's real measurements. Fails OPEN: a
        database write failure is logged as a WARNING and swallowed,
        never raised -- this is recording data ABOUT a build, and a
        write failure here must never abort (or appear to fail) the
        build itself. `sample` is validated BEFORE the write attempt so
        malformed data never gets silently persisted and then invisible
        to `get_k()`'s `source_bytes > 0` filter.
        """
        _require_valid_sample(sample)

        def operation(conn: sqlite3.Connection) -> None:
            conn.execute(
                """
                INSERT INTO xray_graph_k_calibration_samples
                    (language, source_bytes, decls, call_sites, candidate_edges, actual_peak_rss, recorded_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    sample.language,
                    sample.source_bytes,
                    sample.decls,
                    sample.call_sites,
                    sample.candidate_edges,
                    sample.actual_peak_rss,
                    time.time(),
                ),
            )

        try:
            self._conn_manager.execute_atomic(operation)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "SqliteKCalibrationBackend.record_sample: write failed (fail-open): %s",
                exc,
            )

    def get_k(self, language: str) -> Optional[float]:
        """Returns the MAX observed `actual_peak_rss / source_bytes`
        multiplier recorded for `language`, or `None` if no samples
        exist for it. Computed in SQL (MAX of the ratio) rather than
        Python so a future large sample table never has to be pulled
        client-side just to answer one language's worst-case ratio.
        """
        conn = self._conn_manager.get_connection()
        row = conn.execute(
            """
            SELECT MAX(CAST(actual_peak_rss AS REAL) / source_bytes)
            FROM xray_graph_k_calibration_samples
            WHERE language = ? AND source_bytes > 0
            """,
            (language,),
        ).fetchone()
        if row is None or row[0] is None:
            return None
        return float(row[0])


class KStoreProtocol(Protocol):
    """The one method `TTLCachedKProvider` needs from either backend
    (`SqliteKCalibrationBackend`, `XrayGraphKCalibrationPostgresBackend`)
    -- satisfied structurally, without either inheriting from this.
    """

    def get_k(self, language: str) -> Optional[float]: ...


class TTLCachedKProvider:
    """Node-local TTL cache in front of ANY K-calibration store. Exists
    purely to keep the calibration read off the admission hot path (AC12
    Gate 1/Gate 2 run per graph build); it never permits divergence
    between nodes, since the underlying store is always the SAME
    cluster-shared database and this cache only ever shortens HOW OFTEN
    each node re-reads it.

    A read failure (store exception) or a genuine no-sample miss are
    both cached as `None` for the TTL window too -- a broken or
    not-yet-populated store must not be hammered every single call, and
    both cases degrade identically to the caller-supplied conservative
    default via `get_k()`. `_lock` guards the shared cache dict against
    concurrent readers (multiple request threads may call `get_k()`
    simultaneously in server mode).
    """

    def __init__(
        self,
        store: KStoreProtocol,
        *,
        ttl_seconds: float,
        time_fn: Optional[Callable[[], float]] = None,
    ) -> None:
        if (
            not isinstance(ttl_seconds, (int, float))
            or not math.isfinite(ttl_seconds)
            or ttl_seconds < 0
        ):
            raise ValueError(
                f"ttl_seconds must be a finite, non-negative number, got {ttl_seconds!r}"
            )
        self._store = store
        self._ttl_seconds = ttl_seconds
        self._time_fn: Callable[[], float] = (
            time_fn if time_fn is not None else time.monotonic
        )
        self._cache: Dict[str, tuple] = {}
        self._lock = threading.Lock()

    def get_k(self, language: str, conservative_default: float) -> float:
        now = self._time_fn()
        with self._lock:
            cached = self._cache.get(language)
            if cached is not None:
                fetched_at, value = cached
                if now - fetched_at < self._ttl_seconds:
                    return value if value is not None else conservative_default

        try:
            value = self._store.get_k(language)
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "TTLCachedKProvider.get_k: store read failed (degrading to conservative default): %s",
                exc,
            )
            value = None

        with self._lock:
            self._cache[language] = (now, value)
        return value if value is not None else conservative_default


# ---------------------------------------------------------------------------
# Process-level singleton — None until server startup installs it.
#
# H4/H6 remediation: mirrors memory_governor.py's own
# get/set/clear_memory_governor() pattern exactly, so service_init.py can
# install the ONE TTLCachedKProvider instance wrapping the storage-mode-
# appropriate K-calibration backend, and a future graph-build call site
# retrieves that SAME instance rather than constructing a second,
# independently-TTL'd provider.
# ---------------------------------------------------------------------------

_xray_k_provider: Optional[TTLCachedKProvider] = None
_xray_k_provider_lock = threading.Lock()


def get_xray_k_provider() -> Optional[TTLCachedKProvider]:
    """Return the process-level K-calibration provider, or None
    (CLI/pre-init)."""
    with _xray_k_provider_lock:
        return _xray_k_provider


def set_xray_k_provider(provider: TTLCachedKProvider) -> None:
    """Install the process-level K-calibration provider (called once in
    server service_init)."""
    global _xray_k_provider
    with _xray_k_provider_lock:
        _xray_k_provider = provider


def clear_xray_k_provider() -> None:
    """Clear the process-level K-calibration provider (lifespan shutdown /
    test isolation)."""
    global _xray_k_provider
    with _xray_k_provider_lock:
        _xray_k_provider = None
