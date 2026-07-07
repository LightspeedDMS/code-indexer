"""Process-level registry for the temporal metadata backend factory.

Bug #1313: mirrors the coalescer_registry.py pattern
(server/services/coalescer_registry.py) -- a process-level singleton holding
an OPTIONAL factory callable. CLI, daemon, and solo server modes NEVER call
``set_temporal_metadata_backend_factory``, so ``get_temporal_metadata_backend_factory``
stays ``None`` there and ``TemporalMetadataStore`` falls back to the SQLite
backend (byte-for-byte unchanged CLI behavior).

Cluster/postgres server startup (lifespan.py) calls
``set_temporal_metadata_backend_factory`` ONCE, after the PostgreSQL
connection pool is available, so every subsequent ``TemporalMetadataStore``
construction routes through ``TemporalMetadataPostgresBackend`` instead --
eliminating the NFS-backed SQLite-WAL bottleneck (Bug #1313 root cause).

This module lives in the CORE layer (code_indexer.storage) and MUST NEVER
import anything from code_indexer.server.* -- see the layering guard test
(test_temporal_metadata_layering_guard.py). The factory itself (built and
injected from server startup) is free to close over server-only objects; the
registry only stores an opaque ``Callable[[Path], TemporalMetadataBackend]``.
"""

import threading
from pathlib import Path
from typing import TYPE_CHECKING, Callable, Optional

if TYPE_CHECKING:  # pragma: no cover - typing only, avoids a runtime import cycle
    from .temporal_metadata_backend import TemporalMetadataBackend

# Bug #1313 round-3: cross-process IPC-of-a-bootstrap-path contract.
#
# The server lifespan process installs the PG temporal factory ONLY in its
# own process (via set_temporal_metadata_backend_factory below); cluster
# temporal indexing actually runs in a CHILD `cidx index --index-commits`
# subprocess (spawned via Popen by golden_repo_manager.py / refresh_scheduler.py),
# which never called set_temporal_metadata_backend_factory -- so it silently
# fell back to the SQLite backend and recreated temporal_metadata.db on the
# NFS-backed golden-repos mount (the exact bottleneck Bug #1313 fixes).
#
# TEMPORAL_PG_BOOTSTRAP_DIR_ENV names the env var the parent sets on the
# child's environment -- ONLY when the server's storage_mode == "postgres",
# ONLY for the two temporal-index Popen calls -- carrying the server's
# resolved server_dir (the directory containing config.json), NEVER the DSN
# itself: argv and env are both world-readable via /proc/<pid>/cmdline /
# /proc/<pid>/environ, so passing only a path avoids duplicating the secret
# and avoids a second source of truth on credential rotation. The child
# re-reads storage_mode + postgres_dsn from config.json at that path (see
# code_indexer.server.storage.postgres.temporal_child_wiring). Presence of
# this env var means "install the PG temporal backend from bootstrap config
# at this dir before constructing any TemporalMetadataStore"; absence means
# today's SQLite behavior (CLI/solo byte-unchanged).
TEMPORAL_PG_BOOTSTRAP_DIR_ENV = "CIDX_TEMPORAL_PG_BOOTSTRAP_DIR"

# Process-level singleton. None until server lifespan (postgres mode) sets it.
_factory: Optional[Callable[[Path], "TemporalMetadataBackend"]] = None
_factory_lock = threading.Lock()


def get_temporal_metadata_backend_factory() -> Optional[
    Callable[[Path], "TemporalMetadataBackend"]
]:
    """Return the process-level backend factory, or None if none was set.

    ``None`` is the CLI/solo case and the pre-lifespan case -- callers
    (``TemporalMetadataStore.__init__``) treat it as "use the SQLite backend".
    """
    with _factory_lock:
        return _factory


def set_temporal_metadata_backend_factory(
    factory: Callable[[Path], "TemporalMetadataBackend"],
) -> None:
    """Install the process-level backend factory (called once in lifespan startup)."""
    global _factory
    with _factory_lock:
        _factory = factory


def clear_temporal_metadata_backend_factory() -> None:
    """Clear the process-level backend factory (lifespan shutdown / test isolation)."""
    global _factory
    with _factory_lock:
        _factory = None


def install_poison_temporal_metadata_backend_factory(reason: str) -> None:
    """Install a factory that raises a clear operational error on first use.

    Bug #1313 round-2 rework (Codex Finding A): postgres/cluster-mode startup
    must NEVER leave the registry factory unset on failure. ``get_...factory()
    is None`` is the CLI/solo sentinel for "use the SQLite backend" (see
    ``TemporalMetadataStore.__init__``) -- so leaving it unset in postgres
    mode after a wiring failure silently reintroduces the NFS-backed
    SQLite-WAL Cluster-Aware-State violation this bug exists to fix.

    Call this from postgres-mode startup's failure paths instead of leaving
    the factory unset: server startup itself still survives (mirrors this
    codebase's established non-fatal startup convention -- e.g. the
    coalescer registry and ConfigService pool wiring in lifespan.py), but
    every subsequent ``TemporalMetadataStore`` construction raises loudly and
    actionably instead of silently degrading to SQLite-on-NFS.

    Args:
        reason: Human-readable explanation of why the real PostgreSQL
            factory could not be installed, included verbatim in the
            RuntimeError message raised on use.
    """

    def _poison_factory(collection_path: Path) -> "TemporalMetadataBackend":
        raise RuntimeError(
            f"Bug #1313: PostgreSQL temporal metadata backend is unavailable "
            f"in postgres/cluster mode ({reason}). Refusing to silently fall "
            f"back to the NFS-backed SQLite-WAL backend (violates the "
            f"Cluster-Aware State invariant -- see CLAUDE.md). Temporal "
            f"indexing/query for collection '{collection_path}' is "
            f"unavailable until this is fixed and the server is restarted."
        )

    set_temporal_metadata_backend_factory(_poison_factory)
