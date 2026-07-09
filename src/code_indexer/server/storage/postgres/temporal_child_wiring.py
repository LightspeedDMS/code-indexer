"""Cross-process temporal-metadata PostgreSQL backend wiring (Bug #1313 round-3).

Root cause (Codex round-3 finding): the round-1/round-2 fix installed the
PostgreSQL temporal-metadata factory ONLY in the server's own lifespan
process (see server/startup/lifespan.py). But cluster temporal indexing
actually runs in a CHILD `cidx index --index-commits` subprocess, spawned via
Popen by golden_repo_manager.py (registration/refresh) and
refresh_scheduler.py (scheduled refresh). That child's CLI entrypoint
(cli.py's standalone `if index_commits:` branch) constructs
FilesystemVectorStore -- and therefore TemporalMetadataStore -- without ever
calling set_temporal_metadata_backend_factory, so
get_temporal_metadata_backend_factory() returned None there and the child
silently fell back to the SQLite backend, recreating temporal_metadata.db on
the NFS-backed golden-repos mount (the exact bottleneck Bug #1313 exists to
fix). The round-1/round-2 PG plumbing was therefore inert on the real
cluster hot path.

This module closes that gap with a minimal, path-only IPC contract:

  1. build_temporal_child_env(server_config, base_env=None): PARENT side.
     Returns an env dict with CIDX_TEMPORAL_PG_BOOTSTRAP_DIR set to the
     server's resolved server_dir (the directory containing config.json)
     ONLY when storage_mode == "postgres"; otherwise returns None (caller
     passes env=None -- unchanged env, unchanged SQLite behavior). The DSN
     itself NEVER crosses via argv or env: both are world-readable via
     /proc/<pid>/cmdline and /proc/<pid>/environ, so passing only a path
     avoids duplicating the secret and avoids a second source of truth on
     credential rotation.

  2. install_postgres_temporal_backend_from_bootstrap(bootstrap_dir): CHILD
     side. Re-reads storage_mode + postgres_dsn from config.json at
     bootstrap_dir (the SAME config.json the parent server process reads
     from) and installs a REAL PostgreSQL-backed factory via the shared
     make_postgres_temporal_metadata_factory definition (guaranteeing an
     identical collection_key formula to the server's own in-process
     wiring). Any misconfiguration (missing config, wrong storage_mode, no
     DSN) raises RuntimeError immediately -- FAIL LOUD, no poison-factory
     fallback here: the child is single-purpose (either it can index
     against real PostgreSQL, or it must refuse to run at all), unlike the
     long-lived server process which stays up to serve other traffic.
"""

from __future__ import annotations

import os
from typing import Any, Dict, Optional

from code_indexer.server.storage.postgres.connection_pool import ConnectionPool
from code_indexer.server.storage.postgres.temporal_metadata_backend import (
    make_postgres_temporal_metadata_factory,
)
from code_indexer.server.utils.config_manager import ServerConfig, ServerConfigManager
from code_indexer.storage.temporal_metadata_backend_registry import (
    TEMPORAL_PG_BOOTSTRAP_DIR_ENV,
    set_temporal_metadata_backend_factory,
)

# Bounded connect/acquire timeout for the child's dedicated pool -- this is
# infra (connection establishment), NOT an indexing-work timeout (Bug #1218:
# no wall-clock timeout is ever applied to the indexing work itself).
_TEMPORAL_CHILD_POOL_MIN_SIZE = 1
_TEMPORAL_CHILD_POOL_MAX_SIZE = 8
_TEMPORAL_CHILD_POOL_TIMEOUT_SECONDS = 30.0


def build_temporal_child_env(
    server_config: Optional[ServerConfig], base_env: Optional[Dict[str, str]] = None
) -> Optional[Dict[str, str]]:
    """Build the env dict for a temporal-indexing child Popen call.

    Args:
        server_config: The server's own ServerConfig, or None if unavailable
            (bootstrap read failed -- treated the same as sqlite mode: no
            special wiring, child gets the SQLite default).
        base_env: Environment to merge into (copied, never mutated). When
            None, defaults to a copy of the current process's os.environ so
            the child inherits PATH and everything else it needs.

    Returns:
        None when server_config is None or storage_mode != "postgres" (the
        caller then passes env=None to Popen -- fully unchanged behavior).
        Otherwise a NEW dict (base_env or os.environ, copied) with
        CIDX_TEMPORAL_PG_BOOTSTRAP_DIR set to server_config.server_dir.
    """
    if server_config is None or server_config.storage_mode != "postgres":
        return None

    merged: Dict[str, str] = (
        dict(base_env) if base_env is not None else dict(os.environ)
    )
    merged[TEMPORAL_PG_BOOTSTRAP_DIR_ENV] = server_config.server_dir
    return merged


def install_postgres_temporal_backend_from_bootstrap(bootstrap_dir: str) -> Any:
    """Install the PG temporal-metadata factory in THIS (child) process.

    Re-reads storage_mode + postgres_dsn from config.json at bootstrap_dir
    (never trusts anything passed via argv/env beyond the path itself) and,
    on success, installs a real PostgreSQL-backed factory via
    set_temporal_metadata_backend_factory so every subsequent
    TemporalMetadataStore construction in this process routes through
    PostgreSQL -- eliminating the NFS-backed SQLite-WAL bottleneck for the
    actual indexing writes performed by this child subprocess.

    FAIL LOUD on any misconfiguration: this function raises RuntimeError
    rather than installing a poison factory (unlike the server's own
    lifespan wiring). The child is single-purpose -- either it indexes
    against real PostgreSQL or it must not run at all; there is no other
    traffic on this process for a poison factory to protect.

    Args:
        bootstrap_dir: Absolute path to the server's resolved server_dir
            (the directory containing config.json), as set by the parent
            via CIDX_TEMPORAL_PG_BOOTSTRAP_DIR / build_temporal_child_env.

    Returns:
        The ConnectionPool this function created -- the caller (cli.py) is
        responsible for closing it (and clearing the registry factory) in a
        finally block once indexing completes.

    Raises:
        RuntimeError: config.json is missing/unreadable at bootstrap_dir,
            storage_mode != "postgres", or postgres_dsn is blank/None.
    """
    server_config = ServerConfigManager(server_dir_path=bootstrap_dir).load_config()

    if server_config is None:
        raise RuntimeError(
            f"Bug #1313: CIDX_TEMPORAL_PG_BOOTSTRAP_DIR points at "
            f"'{bootstrap_dir}', but no readable config.json was found "
            f"there. Cannot initialize the PostgreSQL temporal metadata "
            f"backend required in cluster mode."
        )

    if server_config.storage_mode != "postgres":
        raise RuntimeError(
            f"Bug #1313: CIDX_TEMPORAL_PG_BOOTSTRAP_DIR points at "
            f"'{bootstrap_dir}', but that server's config.json has "
            f"storage_mode='{server_config.storage_mode}' (expected "
            f"'postgres'). Refusing to proceed -- the parent server "
            f"believes it is in postgres/cluster mode but the bootstrap "
            f"config disagrees."
        )

    if not server_config.postgres_dsn:
        raise RuntimeError(
            f"Bug #1313: config.json at '{bootstrap_dir}' has "
            f"storage_mode='postgres' but postgres_dsn is missing/blank. "
            f"Cannot initialize the PostgreSQL temporal metadata backend "
            f"required in cluster mode."
        )

    pool = ConnectionPool(
        server_config.postgres_dsn,
        min_size=_TEMPORAL_CHILD_POOL_MIN_SIZE,
        max_size=_TEMPORAL_CHILD_POOL_MAX_SIZE,
        timeout=_TEMPORAL_CHILD_POOL_TIMEOUT_SECONDS,
        name="temporal-child",
    )
    set_temporal_metadata_backend_factory(make_postgres_temporal_metadata_factory(pool))
    return pool
