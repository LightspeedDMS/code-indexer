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

Modular plan phase 4c: part of the cidx-xray feature. Both backends work over
a connection factory -- the feature's ``storage`` capability, which is the
server database (SQLite solo, the PostgreSQL pool in cluster mode) -- and the
table is created by the feature's migration 1 (``cidx_xray.server.migrations``,
formerly the core's PostgreSQL migration 051). In solo mode the samples now
live in the server database instead of a dedicated ``xray_k_calibration.db``;
samples recorded there before are not carried over (K degrades to the
conservative seed until new builds calibrate it, and nothing reads K yet).
"""

from __future__ import annotations

import logging
import math
import threading
import time
from contextlib import AbstractContextManager
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional, Protocol

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


#: A DB-API connection factory: ``connect()`` is a context manager that yields
#: a connection and commits when the block ends normally (the ``storage``
#: capability's ``connection``).
ConnectionFactory = Callable[[], AbstractContextManager[Any]]


class SqliteKCalibrationBackend:
    """SQLite backend for K calibration samples (solo deployment).

    Reads and writes the server database through *connect*; the table is
    created by the feature's migration, never here.
    """

    def __init__(self, connect: ConnectionFactory) -> None:
        if not callable(connect):
            raise ValueError("connect must be a connection factory")
        self._connect = connect

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
        try:
            with self._connect() as conn:
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
        with self._connect() as conn:
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
# get/set/clear_memory_governor() pattern exactly, so the feature's
# on_startup can install the ONE TTLCachedKProvider instance wrapping the
# storage-mode-appropriate K-calibration backend, and a future graph-build
# call site retrieves that SAME instance rather than constructing a second,
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
    """Install the process-level K-calibration provider (called once by the
    feature's on_startup)."""
    global _xray_k_provider
    with _xray_k_provider_lock:
        _xray_k_provider = provider


def clear_xray_k_provider() -> None:
    """Clear the process-level K-calibration provider (feature shutdown /
    test isolation)."""
    global _xray_k_provider
    with _xray_k_provider_lock:
        _xray_k_provider = None
