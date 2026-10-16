"""PostgreSQL backend for xray evaluator cache (cluster mode) — Epic #1019.

Stores compiled .so blobs in PostgreSQL so all cluster nodes share the same
compiled evaluator cache, avoiding per-node recompilation.

Table: xray_evaluator_cache
  source_hash   TEXT PRIMARY KEY  — opaque cache identity (see below)
  rustc_version TEXT NOT NULL     — rustc version string (ABI guard)
  so_bytes      BYTEA NOT NULL    — integrity envelope around the .so
  compiled_at   TIMESTAMPTZ       — when the .so was compiled
  compile_ms    BIGINT            — compilation time in milliseconds

<<<<<<< HEAD
`so_bytes` is NOT a loadable `.so`. It is the opaque envelope produced by
`rust_backend.wrap_so_for_cluster_cache` — magic + digest + payload, with the
digest bound to the `source_hash` key and the rustc version — and it is
verified by `rust_backend.verified_so_from_cluster_blob` at the moment a
reader would turn it into a file Rust `dlopen`s. This table stores and
returns it byte-for-byte and never interprets it; a row whose bytes do not
verify is simply not served to the loader, and the next local compile
overwrites it through the upsert below. Rows written before the envelope
existed carry a bare `.so`, fail verification once, and are rewritten the
same way — no migration, no backfill, no flag day.

TTL is enforced ON READ ONLY: fetch() passes a cutoff timestamp in the WHERE
clause so an expired row can never be SERVED, regardless of whether it has been
physically deleted yet. The TTL bounds CAPACITY, not correctness -- see the
XRAY_CACHE_TTL_SECONDS comment for why a row can never go stale in the sense of
becoming wrong.

CAPACITY is bounded by two sweeps, both run as a side effect of store():
_cleanup_expired() (rows past the TTL) and _evict_over_capacity() (rows
beyond the XRAY_CACHE_MAX_ROWS most recent). The row cap, not the TTL, is
what actually keeps this table inside the 10 GiB single-replica PVC -- a
busy cluster can hold far more rows alive inside a 7-day TTL than the
volume holds, and a full PostgreSQL refuses writes for sessions, jobs,
leases and leader election alike. Both sweeps delete at most
XRAY_CACHE_DELETE_CHUNK_ROWS rows per call so no single store() ever eats
a multi-second stall inside a user's xray query; a backlog drains across
subsequent store() calls.

Deletion is still LAZY -- both sweeps are called only as a side effect of
store(), so an idle cluster (no new evaluator ever compiled) leaves
expired and over-cap rows sitting in the table indefinitely; nothing
proactively sweeps them. This is a correctness/capacity distinction, not
just phrasing: "ages out via TTL" would wrongly imply automatic deletion.
Adding a scheduled cleanup pass is a legitimate future improvement but is
explicitly out of scope here (Bug #1784 review MINOR-4) -- this docstring
exists only to describe current behavior accurately. Note that laziness is
bounded in the direction that matters: rows can only accumulate while
store() is being called, which is precisely when the sweeps run.
=======
TTL is enforced ON READ ONLY: fetch() passes a cutoff timestamp in the WHERE
clause so a stale row can never be SERVED, regardless of whether it has been
physically deleted yet. Deletion itself is LAZY -- _cleanup_expired() is
called only as a side effect of store(), so an idle cluster (no new
evaluator ever compiled) leaves expired rows sitting in the table
indefinitely; nothing proactively sweeps them. This is a correctness/
capacity distinction, not just phrasing: "ages out via TTL" would wrongly
imply automatic deletion. Adding a scheduled cleanup pass is a legitimate
future improvement but is explicitly out of scope here (Bug #1784 review
MINOR-4) -- this docstring exists only to describe current behavior
accurately.
>>>>>>> upstream/master

Bug #1784: `source_hash` is treated by this module purely as an OPAQUE TEXT
key — no schema change was needed to fix the bug. The VALUE callers pass is
the composite cache identity (sha256(assembled_source) + XRAY_ABI_VERSION +
rustc_version), computed once in Rust (xray-core's compute_cache_identity /
cache_identity_info) and obtained by RustNativeBackend via
`xray-cli --print-cache-identity` — never independently re-derived here or
in Python. This is deliberate: an ABI-2 node and an ABI-3 node compiling the
identical raw evaluator source now produce DIFFERENT identity values and
occupy different rows instead of overwriting each other via
`ON CONFLICT (source_hash) DO UPDATE`, so old and new nodes coexist safely
for the whole rolling-restart window — old rows can never be SERVED to a
node computing the new identity (TTL-on-read, see above), though they are
only physically deleted the next time some node calls store().

All PostgreSQL exceptions are caught and logged at WARNING level — callers
always receive None (fetch) or a silent no-op (store) on failure so that
solo-mode deployments and transient PG errors degrade gracefully. Swallowed
store failures and every eviction are additionally counted as OTEL metrics
(cidx.xray.cache_store_failures, cidx.xray.cache_rows_evicted,
cidx.xray.cache_eviction_backlog) so that a cluster whose cache has been
failing, or whose table is over its capacity bound and not draining, is
visible as something other than a log line nobody greps.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Any, Dict, Optional

from .connection_pool import ConnectionPool

logger = logging.getLogger(__name__)

# 7 days. This is a CAPACITY bound, not a correctness one, and the two want very
# different numbers.
#
# `source_hash` is not a hash of the evaluator source alone -- it is the composite
# identity sha256(assembled_source) + XRAY_ABI_VERSION + rustc_version (see the
# module docstring), and fetch() filters on rustc_version a second time. So the
# key fully determines the artifact: a given key can only ever map to one correct
# .so, and no passage of time can make a stored row WRONG. A toolchain or ABI
# change does not invalidate rows, it changes the key, which makes the old rows
# unreachable rather than stale -- and unreachable rows are exactly what
# _cleanup_expired() exists to reclaim.
#
# At 300s the TTL was doing the opposite of its job: it could not prevent a wrong
# answer (nothing can produce one), while it guaranteed that any evaluator not
# re-run within five minutes was recompiled from scratch on the next query, on
# every node, forever. That is the cost this cache was built to remove -- and it
# fell hardest on exactly the queries worth caching, the occasional expensive
# ones rather than the hot loop. 7 days keeps the table bounded against toolchain
# churn while letting a compiled evaluator actually be reused.
XRAY_CACHE_TTL_SECONDS = 7 * 24 * 60 * 60  # 7 days

# Hard row cap. The TTL bounds how LONG a row lives; it does not bound how MANY
# live at once, and on this hardware only the second number is survivable.
#
# Arithmetic, from measurements on a real compiled evaluator:
#   * a compiled evaluator .so is 4.28 MiB (floor 4.26 MiB -- the Rust runtime
#     and the xray ABI shim dominate; a trivial evaluator is not smaller)
#   * one stored row costs 1.65 MiB on disk including heap tuple, primary key
#     and TOAST index
#   * the cidx-postgres PVC is 10 GiB = 10240 MiB, ONE replica, gp2, with no
#     autoscaling (cet-cidx-overlay/cluster-base/postgres.yaml)
#
# The cache may claim at most HALF the volume: the other half belongs to the
# coordination plane (sessions, background jobs, leases and LEADER ELECTION),
# the golden-repo registry, WAL, and the free space autovacuum needs to reclaim
# anything at all. A full PostgreSQL does not degrade -- it refuses writes, and
# every one of those subsystems fails at once. That failure would arrive two
# hops from its cause, because store() below deliberately swallows its
# exceptions, so the bound has to be enforced here rather than discovered then.
#
#   budget   = 10240 MiB / 2        = 5120 MiB
#   capacity = 5120 MiB / 1.65 MiB  = 3103 rows  -> round down to 3000
#   worst case 3000 * 1.65 MiB      = 4950 MiB (4.83 GiB), 5.17 GiB left free
#
# 3000 is also deliberately ~2x a realistic single-generation working set. A
# toolchain or ABI bump does not invalidate rows, it changes the cache key, so
# the old and new generations coexist for the whole 7-day TTL; the cap must hold
# both at once or a rolling restart evicts the rows the not-yet-restarted nodes
# are still reading.
XRAY_CACHE_MAX_ROWS = 3000

# Rows deleted per DELETE statement.
#
# A matching DELETE measured 2.0 s per 500 rows -- 4 ms/row, dominated by TOAST
# chunk deletion rather than by finding the rows (an index on compiled_at is NOT
# the problem: the scan measured 1.7 ms, and adding one would not help).
# Unchunked, reclaiming a 3000-row generation would be a ~12 s statement running
# inside whichever user's xray query happened to trigger the store().
#
#   100 rows * 4 ms = 0.4 s per DELETE
#   store() emits at most two (TTL sweep + capacity eviction) = 0.8 s worst case
#
# Each store() therefore reclaims at most 200 rows, and a large backlog drains
# across subsequent store() calls instead of in one stall. That is the right
# trade: a backlog only exists when evaluators are being compiled, which is
# exactly when store() is being called.
XRAY_CACHE_DELETE_CHUNK_ROWS = 100

# OTEL counter cache, keyed by name, invalidated when the resolved meter
# changes. Counters are created lazily on first emission so that merely
# importing this module never pulls in the server telemetry stack (this backend
# is constructed in cluster mode only, but the module is importable from the
# CLI/solo path), and are reused afterwards so repeated store() calls do not
# re-register duplicate instruments with the SDK.
_METRIC_STATE: Dict[str, Any] = {"meter": None, "counters": {}}


def _record_cache_counter(name: str, value: int, reason: str) -> None:
    """Add to an OTEL counter on the cidx.application meter. Never raises.

    Follows the same peek_telemetry_manager() + is_active gating as
    rust_backend._record_identity_failure_metric and the counters in
    telemetry/metrics_instrumentation.py -- same meter, same SDK, same
    activation decision. The instruments are created here rather than as
    ApplicationMetrics methods only because this change is scoped to this
    module; folding them into ApplicationMetrics alongside
    cidx.xray.cache_identity_failures would be a strict improvement and is
    left as a follow-up.
    """
    if value <= 0:
        return
    try:
        from code_indexer.server.telemetry.manager import (  # noqa: PLC0415
            peek_telemetry_manager,
        )
        from code_indexer.server.telemetry.metrics_instrumentation import (  # noqa: PLC0415,E501
            get_application_metrics,
        )

        telemetry_manager = peek_telemetry_manager()
        if telemetry_manager is None:
            return
        if not get_application_metrics(telemetry_manager).is_active:
            return

        meter = telemetry_manager.get_meter("cidx.application")
        if _METRIC_STATE["meter"] is not meter:
            _METRIC_STATE["meter"] = meter
            _METRIC_STATE["counters"] = {}
        counter = _METRIC_STATE["counters"].get(name)
        if counter is None:
            counter = meter.create_counter(
                name=name,
                description=_COUNTER_DESCRIPTIONS[name],
                unit="1",
            )
            _METRIC_STATE["counters"][name] = counter
        counter.add(value, {"reason": reason})
    except Exception as exc:  # telemetry must never break the cache
        logger.debug("XrayCachePostgresBackend: metric %s not recorded: %s", name, exc)


_COUNTER_DESCRIPTIONS = {
    "cidx.xray.cache_store_failures": (
        "Number of swallowed xray evaluator-cache write failures"
    ),
    "cidx.xray.cache_rows_evicted": (
        "Number of xray evaluator-cache rows reclaimed, by reason"
    ),
    "cidx.xray.cache_eviction_backlog": (
        "Number of eviction chunks that came back full, meaning the xray "
        "evaluator cache is still above its bound after a store()"
    ),
}


class XrayCachePostgresBackend:
    """Stores compiled .so blobs in PostgreSQL for cross-node sharing."""

    def __init__(
        self,
        pool: ConnectionPool,
        *,
        max_rows: int = XRAY_CACHE_MAX_ROWS,
        delete_chunk_rows: int = XRAY_CACHE_DELETE_CHUNK_ROWS,
    ) -> None:
        self._pool = pool
        self._max_rows = max_rows
        self._delete_chunk_rows = delete_chunk_rows
        self._ensure_schema()

    def _ensure_schema(self) -> None:
        """Create xray_evaluator_cache table if it does not already exist."""
        try:
            with self._pool.connection() as conn:
                conn.execute("""
                    CREATE TABLE IF NOT EXISTS xray_evaluator_cache (
                        source_hash     TEXT PRIMARY KEY,
                        rustc_version   TEXT NOT NULL,
                        so_bytes        BYTEA NOT NULL,
                        compiled_at     TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                        compile_ms      BIGINT NOT NULL DEFAULT 0
                    )
                """)
                conn.commit()
        except Exception as exc:
            logger.warning("XrayCachePostgresBackend: schema setup failed: %s", exc)

    def fetch(
        self,
        source_hash: str,
        rustc_version: str,
        ttl_seconds: int = XRAY_CACHE_TTL_SECONDS,
    ) -> Optional[bytes]:
        """Return cached .so bytes if fresh and rustc_version matches, else None."""
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(seconds=ttl_seconds)
            with self._pool.connection() as conn:
                row = conn.execute(
                    """
                    SELECT so_bytes FROM xray_evaluator_cache
                    WHERE source_hash = %s
                      AND rustc_version = %s
                      AND compiled_at > %s
                    """,
                    (source_hash, rustc_version, cutoff),
                ).fetchone()
                return row[0] if row else None
        except Exception as exc:
            logger.warning("XrayCachePostgresBackend: fetch failed: %s", exc)
            return None

    def store(
        self,
        source_hash: str,
        rustc_version: str,
        so_bytes: bytes,
        compile_ms: int = 0,
    ) -> None:
        """Upsert compiled .so bytes, then run lazy TTL cleanup."""
        try:
            with self._pool.connection() as conn:
                conn.execute(
                    """
                    INSERT INTO xray_evaluator_cache
                        (source_hash, rustc_version, so_bytes, compiled_at, compile_ms)
                    VALUES (%s, %s, %s, NOW(), %s)
                    ON CONFLICT (source_hash) DO UPDATE SET
                        rustc_version = EXCLUDED.rustc_version,
                        so_bytes = EXCLUDED.so_bytes,
                        compiled_at = NOW(),
                        compile_ms = EXCLUDED.compile_ms
                    """,
                    (source_hash, rustc_version, so_bytes, compile_ms),
                )
                conn.commit()
        except Exception as exc:
            logger.warning("XrayCachePostgresBackend: store failed: %s", exc)
            # Still swallowed -- this is a cache, and a cluster that cannot
            # write it must keep answering queries by recompiling. But a node
            # whose every write has failed for a week was previously visible
            # only as a WARNING nobody greps, so count it too.
            _record_cache_counter("cidx.xray.cache_store_failures", 1, "exception")
            return
        # Two bounds, both chunked. The TTL sweep reclaims rows made unreachable
        # by a toolchain or ABI bump; the capacity eviction is what actually
        # keeps this table off the PVC ceiling, because a busy cluster can hold
        # far more than 3000 rows alive inside a 7-day TTL.
        self._cleanup_expired()
        self._evict_over_capacity()

    def _cleanup_expired(
        self,
        ttl_seconds: int = XRAY_CACHE_TTL_SECONDS,
        chunk_rows: Optional[int] = None,
    ) -> int:
        """Delete up to one chunk of rows older than TTL. Returns count deleted.

        Oldest first, so a backlog is reclaimed in the order it stopped being
        useful. One chunk per call, never a loop: see
        XRAY_CACHE_DELETE_CHUNK_ROWS for why an unchunked delete here is a
        multi-second stall inside a user's xray query.
        """
        limit = chunk_rows if chunk_rows is not None else self._delete_chunk_rows
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(seconds=ttl_seconds)
            with self._pool.connection() as conn:
                result = conn.execute(
                    """
                    DELETE FROM xray_evaluator_cache
                    WHERE source_hash IN (
                        SELECT source_hash FROM xray_evaluator_cache
                        WHERE compiled_at <= %s
                        ORDER BY compiled_at ASC, source_hash ASC
                        LIMIT %s
                    )
                    """,
                    (cutoff, limit),
                )
                conn.commit()
                deleted = result.rowcount or 0
        except Exception as exc:
            logger.warning("XrayCachePostgresBackend: cleanup failed: %s", exc)
            return 0
        _record_cache_counter("cidx.xray.cache_rows_evicted", deleted, "ttl")
        if deleted >= limit > 0:
            _record_cache_counter("cidx.xray.cache_eviction_backlog", 1, "ttl")
        return deleted

    def _evict_over_capacity(
        self,
        max_rows: Optional[int] = None,
        chunk_rows: Optional[int] = None,
    ) -> int:
        """Delete up to one chunk of rows beyond the N most recent by
        compiled_at. Returns count deleted.

        This is the bound that matters. The TTL limits how long a row lives,
        not how many live at once, and the 10 GiB single-replica PVC is sized
        for the latter -- see XRAY_CACHE_MAX_ROWS for the arithmetic. Evicting
        the least recently compiled row is the right victim choice: compiled_at
        is refreshed on every store() for a given key, so it tracks recency of
        use for anything still being recompiled.
        """
        keep = max_rows if max_rows is not None else self._max_rows
        limit = chunk_rows if chunk_rows is not None else self._delete_chunk_rows
        try:
            with self._pool.connection() as conn:
                result = conn.execute(
                    """
                    DELETE FROM xray_evaluator_cache
                    WHERE source_hash IN (
                        SELECT source_hash FROM xray_evaluator_cache
                        ORDER BY compiled_at DESC, source_hash DESC
                        OFFSET %s
                        LIMIT %s
                    )
                    """,
                    (keep, limit),
                )
                conn.commit()
                deleted = result.rowcount or 0
        except Exception as exc:
            logger.warning(
                "XrayCachePostgresBackend: capacity eviction failed: %s", exc
            )
            return 0
        _record_cache_counter("cidx.xray.cache_rows_evicted", deleted, "capacity")
        if deleted >= limit > 0:
            # The chunk came back full, so rows remain above the cap and one
            # store() could not drain them. This is the alertable signal: the
            # table is over its capacity budget, which is the condition that
            # ends in a full PVC.
            _record_cache_counter("cidx.xray.cache_eviction_backlog", 1, "capacity")
        return deleted
