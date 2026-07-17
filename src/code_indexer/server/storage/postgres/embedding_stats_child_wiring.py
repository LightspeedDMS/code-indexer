"""Cross-process embedding-stats bootstrap wiring -- Story #1418.

Mirrors Bug #1313's ``temporal_child_wiring.py`` parent/child env-var IPC
contract for a DIFFERENT purpose. Temporal's contract exists to cross a
PostgreSQL backend-registry factory across a process boundary (server
lifespan process -> `cidx index --index-commits` child) and therefore only
fires in postgres/cluster mode -- sqlite/solo mode needs no such crossing.

This contract exists to solve a pure DISCOVERY problem: a `cidx index`
child subprocess spawned by the server has NO other way to locate the
server's data directory (needed to open the shared embedding_call_stats
table, SQLite or PostgreSQL) at all. It therefore fires UNCONDITIONALLY for
BOTH SQLite solo AND PostgreSQL cluster storage modes.

  1. build_embedding_stats_child_env(server_config, base_env=None): PARENT
     side. Returns an env dict with CIDX_EMBEDDING_STATS_BOOTSTRAP_DIR set
     to the server's resolved server_dir, in BOTH storage modes. Only
     server_config being None (bootstrap read failed / no server context)
     yields a plain, unmodified copy of base_env/os.environ -- the child
     then falls back to NoOpWriter, matching standalone CLI behavior.

  2. install_embedding_stats_writer_from_bootstrap(bootstrap_dir): CHILD
     side. Re-reads storage_mode + (for postgres) postgres_dsn from
     config.json at bootstrap_dir and installs a real
     CrossProcessBootstrapWriter backed by the matching
     EmbeddingCallStats*Backend. Unlike temporal's child-side installer,
     this function is FAIL-OPEN, never fail-loud: a stats side-channel
     misconfiguration must never abort real indexing work (CLAUDE.md's
     fail-open convention for observability data). Any resolution failure
     (missing config, missing/blank postgres_dsn, unsupported
     storage_mode, DB errors) logs a WARNING and installs a NoOpWriter
     instead of raising -- and cleans up any partially-constructed
     resource (started writer thread, raw connection pool) so no
     background resource is ever leaked on the fallback path. Also
     resolves the runtime-DB-only embedding_stats_config (flush interval +
     enabled) ONCE here via _resolve_embedding_stats_config() -- a direct,
     read-only SQL query against the server_config runtime row, never via
     ConfigService.initialize_runtime_db()/set_connection_pool() (those
     perform destructive first-boot migration/seeding side effects unsafe
     to trigger from a child's read-only bootstrap path). A disabled
     config installs NoOpWriter directly.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from dataclasses import fields
from pathlib import Path
from typing import Dict, Optional

from code_indexer.server.services.embedding_call_stats import (
    EmbeddingCallStatsPostgresBackend,
    EmbeddingCallStatsSqliteBackend,
)
from code_indexer.server.services.embedding_stats_writer import (
    CrossProcessBootstrapWriter,
    EmbeddingStatsWriter,
    NoOpWriter,
    _DEFAULT_FLUSH_INTERVAL_SECONDS,
)
from code_indexer.server.storage.postgres.connection_pool import ConnectionPool
from code_indexer.server.utils.config_manager import (
    EmbeddingStatsConfig,
    ServerConfig,
    ServerConfigManager,
)

logger = logging.getLogger(__name__)

EMBEDDING_STATS_BOOTSTRAP_DIR_ENV = "CIDX_EMBEDDING_STATS_BOOTSTRAP_DIR"

# Bounded connect/acquire timeout for the child's dedicated pool -- infra
# (connection establishment), NOT an indexing-work timeout (Bug #1218: no
# wall-clock timeout is ever applied to the indexing work itself).
_CHILD_POOL_MIN_SIZE = 1
_CHILD_POOL_MAX_SIZE = 4
_CHILD_POOL_TIMEOUT_SECONDS = 30.0

# Mirrors config_service.py's CONFIG_KEY_RUNTIME -- duplicated as a literal
# rather than imported to avoid pulling the full ConfigService module (with
# its ClaudeDelegationManager/DbOutageThrottle construction-time
# dependencies) into this lightweight child-process read path.
_CONFIG_KEY_RUNTIME = "runtime"


def _resolve_embedding_stats_config(
    *,
    storage_mode: str,
    db_path: Optional[str],
    pool: Optional[ConnectionPool],
) -> Optional[EmbeddingStatsConfig]:
    """Read-only, fail-open resolution of the runtime-DB-only
    embedding_stats_config (flush_interval_seconds + enabled).

    Deliberately does NOT use ConfigService.initialize_runtime_db() /
    set_connection_pool() -- those perform destructive first-boot
    migration/seeding side effects (writing to the runtime DB, stripping
    config.json) that must never be triggered from a child subprocess's
    read-only bootstrap path. Instead this queries the `server_config`
    runtime row directly, exactly as ConfigService's own
    _load_runtime_from_sqlite/_load_runtime_from_pg do internally.

    Returns None (fail-open default) when the table/row doesn't exist yet,
    the JSON has no embedding_stats_config key, or any error occurs --
    callers fall back to _DEFAULT_FLUSH_INTERVAL_SECONDS / enabled=True.
    """
    try:
        if storage_mode == "sqlite":
            if not db_path or not Path(db_path).exists():
                return None
            conn = sqlite3.connect(db_path)
            try:
                row = conn.execute(
                    "SELECT config_json FROM server_config WHERE config_key = ?",
                    (_CONFIG_KEY_RUNTIME,),
                ).fetchone()
            finally:
                conn.close()
        else:  # postgres
            if pool is None:
                return None
            with pool.connection() as conn:
                row = conn.execute(
                    "SELECT config_json FROM server_config WHERE config_key = %s",
                    (_CONFIG_KEY_RUNTIME,),
                ).fetchone()

        if row is None:
            return None
        config_json = row[0]
        runtime_dict = (
            json.loads(config_json) if isinstance(config_json, str) else config_json
        )
        es_dict = runtime_dict.get("embedding_stats_config")
        if not isinstance(es_dict, dict):
            return None
        allowed = {f.name for f in fields(EmbeddingStatsConfig)}
        return EmbeddingStatsConfig(
            **{k: v for k, v in es_dict.items() if k in allowed}
        )
    except Exception as exc:
        logger.debug(
            "_resolve_embedding_stats_config: read failed, falling back to "
            "defaults (enabled=True, flush_interval=%s): %s",
            _DEFAULT_FLUSH_INTERVAL_SECONDS,
            exc,
        )
        return None


def build_embedding_stats_child_env(
    server_config: Optional[ServerConfig], base_env: Optional[Dict[str, str]] = None
) -> Dict[str, str]:
    """Build the env dict for a `cidx index` child Popen call.

    Args:
        server_config: The server's own ServerConfig, or None if unavailable.
        base_env: Environment to merge into (copied, never mutated). When
            None, defaults to a copy of the current process's os.environ.

    Returns:
        A NEW dict (base_env or os.environ, copied). When server_config is
        not None, CIDX_EMBEDDING_STATS_BOOTSTRAP_DIR is set to
        server_config.server_dir -- in BOTH sqlite and postgres storage
        modes (unlike build_temporal_child_env, this is unconditional on
        storage_mode). When server_config is None, the dict is returned
        unchanged (no bootstrap var -- the child falls back to NoOpWriter).
    """
    merged: Dict[str, str] = (
        dict(base_env) if base_env is not None else dict(os.environ)
    )
    if server_config is not None:
        merged[EMBEDDING_STATS_BOOTSTRAP_DIR_ENV] = server_config.server_dir
    return merged


def install_embedding_stats_writer_from_bootstrap(
    bootstrap_dir: str,
) -> EmbeddingStatsWriter:
    """Install the embedding-stats writer in THIS (child) process.

    Re-reads storage_mode + (for postgres) postgres_dsn from config.json at
    bootstrap_dir and installs a started CrossProcessBootstrapWriter backed
    by the matching EmbeddingCallStats*Backend via
    EmbeddingStatsWriter.set_active().

    FAIL OPEN: any resolution failure (missing config.json, missing/blank
    postgres_dsn in postgres mode, unsupported storage_mode, DB errors) is
    caught, logged as a WARNING, and a NoOpWriter is installed instead --
    this function NEVER raises. A stats side-channel is observability-only
    and must never abort or degrade real indexing work. Any
    partially-constructed resource on the fallback path (an already-started
    writer thread, or a raw PostgreSQL connection pool created before the
    writer adopted it) is cleaned up so nothing is ever leaked.

    Returns:
        The installed writer (CrossProcessBootstrapWriter on success,
        NoOpWriter on any failure) -- callers may keep a reference to call
        .stop() in a finally block for a best-effort final flush.
    """
    writer: Optional[CrossProcessBootstrapWriter] = None
    pool: Optional[ConnectionPool] = None
    db_path: Optional[str] = None
    try:
        server_config = ServerConfigManager(server_dir_path=bootstrap_dir).load_config()
        if server_config is None:
            raise RuntimeError(
                f"Story #1418: CIDX_EMBEDDING_STATS_BOOTSTRAP_DIR points at "
                f"'{bootstrap_dir}', but no readable config.json was found there."
            )

        if server_config.storage_mode == "postgres":
            if not server_config.postgres_dsn:
                raise RuntimeError(
                    f"Story #1418: config.json at '{bootstrap_dir}' has "
                    f"storage_mode='postgres' but postgres_dsn is missing/blank."
                )
            pool = ConnectionPool(
                server_config.postgres_dsn,
                min_size=_CHILD_POOL_MIN_SIZE,
                max_size=_CHILD_POOL_MAX_SIZE,
                timeout=_CHILD_POOL_TIMEOUT_SECONDS,
                name="embedding-stats-child",
            )
            backend: object = EmbeddingCallStatsPostgresBackend(pool)
        elif server_config.storage_mode == "sqlite":
            db_path = str(Path(bootstrap_dir) / "data" / "cidx_server.db")
            Path(bootstrap_dir, "data").mkdir(parents=True, exist_ok=True)
            backend = EmbeddingCallStatsSqliteBackend(db_path)
        else:
            raise RuntimeError(
                f"Story #1418: config.json at '{bootstrap_dir}' has "
                f"unsupported storage_mode={server_config.storage_mode!r} "
                f"(expected 'sqlite' or 'postgres')."
            )

        # Story #1418 Phase 3: resolve the runtime-DB-only embedding_stats
        # config (flush interval + enabled) ONCE here at bootstrap, reusing
        # the SAME pool/db_path just constructed above for the backend.
        stats_cfg = _resolve_embedding_stats_config(
            storage_mode=server_config.storage_mode, db_path=db_path, pool=pool
        )
        if stats_cfg is not None and not stats_cfg.enabled:
            # Honor the kill-switch at install time: never construct a real
            # writer/thread at all when disabled.
            if pool is not None:
                pool.close()
            noop = NoOpWriter()
            EmbeddingStatsWriter.set_active(noop)
            logger.info(
                "install_embedding_stats_writer_from_bootstrap: embedding "
                "stats disabled via config -- installing NoOpWriter"
            )
            return noop

        flush_interval_seconds = (
            stats_cfg.flush_interval_seconds
            if stats_cfg is not None
            else _DEFAULT_FLUSH_INTERVAL_SECONDS
        )
        writer = CrossProcessBootstrapWriter(
            backend, flush_interval_seconds=flush_interval_seconds
        )
        writer.start()
        EmbeddingStatsWriter.set_active(writer)
        return writer
    except Exception as exc:
        if writer is not None:
            # writer.start() succeeded but a later step failed -- stop the
            # already-running background thread so we never leak a live
            # writer when falling back to NoOpWriter. This also owns
            # closing `pool` (the writer's backend holds it), so pool is
            # NOT separately closed in this branch.
            writer.stop(timeout=2.0)
        elif pool is not None:
            # The pool was constructed but no writer ever adopted it
            # (backend/writer construction itself failed) -- close it
            # directly so the raw connection pool is never leaked.
            try:
                pool.close()
            except Exception as pool_close_exc:  # pragma: no cover -- defensive
                logger.debug(
                    "install_embedding_stats_writer_from_bootstrap: pool "
                    "close failed during fallback cleanup: %s",
                    pool_close_exc,
                )
        logger.warning(
            "install_embedding_stats_writer_from_bootstrap: falling back to "
            "NoOpWriter (bootstrap_dir=%s): %s",
            bootstrap_dir,
            exc,
        )
        noop = NoOpWriter()
        EmbeddingStatsWriter.set_active(noop)
        return noop
