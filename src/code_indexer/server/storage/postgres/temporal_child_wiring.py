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
     Story #1457 AC6 Finding-1 (round-23 correction): this builder is now
     restructured to ALWAYS return a dict (never None), mirroring
     build_embedding_stats_child_env's unconditional-on-storage_mode
     design. It UNCONDITIONALLY sets CIDX_SERVER_REFRESH_CONTEXT=1 in ALL
     storage modes -- the signal a temporal child uses to distinguish
     "I was spawned by the server" (any storage mode, including a
     solo/SQLite local server) from "I am a genuine standalone CLI
     invocation with no server process at all". The PREVIOUS
     postgres-only-return-else-None design silently dropped this signal
     in solo/SQLite server mode, which is genuinely server-context, not
     standalone-CLI -- that was the bug this correction fixes. It
     continues to additionally set CIDX_TEMPORAL_PG_BOOTSTRAP_DIR to the
     server's resolved server_dir (the directory containing config.json)
     ONLY when storage_mode == "postgres" (unchanged from before). The DSN
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

import logging
import os
from typing import Any, Dict, Optional

from code_indexer.server.services.temporal_reader_capability import (
    MIN_DUAL_LAYOUT_READER_VERSION,
    all_serving_nodes_reader_capable,
)
from code_indexer.server.storage.postgres.connection_pool import ConnectionPool
from code_indexer.server.storage.postgres.temporal_metadata_backend import (
    make_postgres_temporal_metadata_factory,
)
from code_indexer.server.services.config_service import get_config_service
from code_indexer.server.utils.config_manager import ServerConfig, ServerConfigManager
from code_indexer.storage.temporal_metadata_backend_registry import (
    TEMPORAL_PG_BOOTSTRAP_DIR_ENV,
    set_temporal_metadata_backend_factory,
)

logger = logging.getLogger(__name__)

# Bounded connect/acquire timeout for the child's dedicated pool -- this is
# infra (connection establishment), NOT an indexing-work timeout (Bug #1218:
# no wall-clock timeout is ever applied to the indexing work itself).
_TEMPORAL_CHILD_POOL_MIN_SIZE = 1
_TEMPORAL_CHILD_POOL_MAX_SIZE = 8
_TEMPORAL_CHILD_POOL_TIMEOUT_SECONDS = 30.0

#: Story #1457 AC6 Finding-1 (round-23): unconditional server-context
#: marker set on EVERY temporal child, in ALL storage modes -- signals
#: "this child was spawned by the server" (as opposed to a genuine
#: standalone `cidx index` with no server process at all). Distinct from
#: TEMPORAL_PG_BOOTSTRAP_DIR_ENV, which remains postgres-only.
CIDX_SERVER_REFRESH_CONTEXT_ENV = "CIDX_SERVER_REFRESH_CONTEXT"

#: Story #1457 AC1 safety gate (2026-07-23 code review; canonical home
#: moved here 2026-07-24 re-review, Codex finding #4): the CHILD process
#: (temporal_relocation_trigger.py) has no DB access, so it can only ever
#: READ this env var. The PARENT (this function, running in the live
#: server process) is the AUTHORITATIVE source -- it resolves the on/off
#: decision from the config service (never raw os.environ, per CLAUDE.md's
#: "No Environment Variables for Server Settings" rule) and transports the
#: resolved value into the child's environment, exactly like
#: CIDX_SERVER_REFRESH_CONTEXT_ENV above.
CIDX_TEMPORAL_SISTER_RELOCATION_ENABLED_ENV = "CIDX_TEMPORAL_SISTER_RELOCATION_ENABLED"


def build_temporal_child_env(
    server_config: Optional[ServerConfig], base_env: Optional[Dict[str, str]] = None
) -> Dict[str, str]:
    """Build the env dict for a temporal-indexing child Popen call.

    Story #1457 AC6 Finding-1 (round-23 correction): ALWAYS returns a dict
    (never None) -- the previous postgres-only-return-else-None shape
    silently dropped the server-context signal in solo/SQLite server mode,
    which IS genuinely server-context (not standalone-CLI).

    Args:
        server_config: The server's own ServerConfig, or None if unavailable
            (bootstrap read failed). The server-context flag is still set
            unconditionally in this case (the function is only ever called
            from server-side spawn sites); only the postgres-specific
            bootstrap-dir var is skipped, since storage_mode is unknown.
        base_env: Environment to merge into (copied, never mutated). When
            None, defaults to a copy of the current process's os.environ so
            the child inherits PATH and everything else it needs.

    Returns:
        A NEW dict (base_env or os.environ, copied) with
        CIDX_SERVER_REFRESH_CONTEXT set to "1" unconditionally,
        CIDX_TEMPORAL_PG_BOOTSTRAP_DIR set to server_config.server_dir
        additionally when server_config.storage_mode == "postgres", and
        CIDX_TEMPORAL_SISTER_RELOCATION_ENABLED set to "1" when the config
        service reports the AC1 safety gate enabled (2026-07-24 re-review,
        Codex finding #4) AND all_serving_nodes_reader_capable() confirms
        every node currently serving this fleet reports a server_version
        at or above the release that first shipped the dual-layout
        chunks.db resolver and the sister-location temporal shard
        resolver (Story #1461 salvage item #3) -- omitted (not just "0")
        when disabled/unreadable/incapable, matching the gate's own
        default-OFF, fail-safe philosophy. Solo deployments are trivially
        always capable (see all_serving_nodes_reader_capable's own
        docstring); a partial-rollout cluster fleet withholds
        sister-location publication until every node has upgraded.
    """
    merged: Dict[str, str] = (
        dict(base_env) if base_env is not None else dict(os.environ)
    )
    merged[CIDX_SERVER_REFRESH_CONTEXT_ENV] = "1"
    if server_config is not None and server_config.storage_mode == "postgres":
        merged[TEMPORAL_PG_BOOTSTRAP_DIR_ENV] = server_config.server_dir

    # 2026-07-24 round-4 re-review (Codex): unconditionally clear any
    # inherited/stale value FIRST, before resolving config -- base_env or
    # os.environ may already carry "1" from a prior enabled run or an
    # operator's ambient shell. Without this, a disabled config or a
    # config-read exception would leave that stale value in place,
    # letting inherited env state silently act as a fallback authority
    # (the opposite of this gate's claimed fail-safe direction). Only the
    # config-resolved value below may set this key back.
    merged.pop(CIDX_TEMPORAL_SISTER_RELOCATION_ENABLED_ENV, None)

    try:
        sister_relocation_enabled = (
            get_config_service()
            .get_config()
            .indexing_config.temporal_sister_relocation_enabled
        )
    except Exception as exc:
        logger.warning(
            "build_temporal_child_env: failed to read "
            "temporal_sister_relocation_enabled from config service "
            "(non-fatal, gate stays disabled): %s: %s",
            type(exc).__name__,
            exc,
        )
        sister_relocation_enabled = False

    if sister_relocation_enabled:
        # Story #1461 salvage item #3: the operator toggle alone is not
        # enough -- during a rolling deploy a partial-rollout fleet could
        # have a just-upgraded node publish sister-location temporal data
        # (Story #1457 AC6) that an old, not-yet-upgraded node cannot
        # resolve/read. AND the toggle with a fleet-wide reader-capability
        # check, resolved fresh on every call so it self-heals the moment
        # the last node in the fleet finishes upgrading.
        _storage_mode_for_capability = (
            server_config.storage_mode if server_config is not None else ""
        )
        if all_serving_nodes_reader_capable(
            MIN_DUAL_LAYOUT_READER_VERSION, _storage_mode_for_capability
        ):
            merged[CIDX_TEMPORAL_SISTER_RELOCATION_ENABLED_ENV] = "1"
        else:
            logger.warning(
                "build_temporal_child_env: temporal_sister_relocation_enabled "
                "is on but not every serving node reports a reader-capable "
                "server_version (>= %s) -- withholding sister-location "
                "publication for this child to protect a partial-rollout "
                "fleet from an older node reading data it cannot parse.",
                MIN_DUAL_LAYOUT_READER_VERSION,
            )

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
