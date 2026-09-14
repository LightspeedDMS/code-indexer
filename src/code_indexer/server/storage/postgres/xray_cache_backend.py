"""PostgreSQL backend for xray evaluator cache (cluster mode) — Epic #1019.

Stores compiled .so blobs in PostgreSQL so all cluster nodes share the same
compiled evaluator cache, avoiding per-node recompilation.

Table: xray_evaluator_cache
  source_hash   TEXT PRIMARY KEY  — opaque cache identity (see below)
  rustc_version TEXT NOT NULL     — rustc version string (ABI guard)
  so_bytes      BYTEA NOT NULL    — compiled .so content
  compiled_at   TIMESTAMPTZ       — when the .so was compiled
  compile_ms    BIGINT            — compilation time in milliseconds

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
solo-mode deployments and transient PG errors degrade gracefully.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone, timedelta
from typing import Optional

from .connection_pool import ConnectionPool

logger = logging.getLogger(__name__)

XRAY_CACHE_TTL_SECONDS = 300  # 5 minutes


class XrayCachePostgresBackend:
    """Stores compiled .so blobs in PostgreSQL for cross-node sharing."""

    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool
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
            return
        self._cleanup_expired()

    def _cleanup_expired(self, ttl_seconds: int = XRAY_CACHE_TTL_SECONDS) -> int:
        """Delete rows older than TTL. Returns count deleted."""
        try:
            cutoff = datetime.now(timezone.utc) - timedelta(seconds=ttl_seconds)
            with self._pool.connection() as conn:
                result = conn.execute(
                    "DELETE FROM xray_evaluator_cache WHERE compiled_at <= %s",
                    (cutoff,),
                )
                conn.commit()
                return result.rowcount or 0
        except Exception as exc:
            logger.warning("XrayCachePostgresBackend: cleanup failed: %s", exc)
            return 0
