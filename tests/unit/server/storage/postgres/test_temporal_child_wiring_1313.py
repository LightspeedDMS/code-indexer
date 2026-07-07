"""Bug #1313 round-3: cross-process temporal-metadata PG backend wiring.

Codex round-3 review found the round-1/round-2 fix INERT on the real cluster
hot path: the server lifespan process installs the PostgreSQL temporal
factory only in ITS OWN process, but cluster temporal indexing actually runs
in a CHILD `cidx index --index-commits` subprocess (spawned via Popen by
golden_repo_manager.py / refresh_scheduler.py). That child's CLI entrypoint
never called set_temporal_metadata_backend_factory, so
get_temporal_metadata_backend_factory() returned None there, and the child
silently used the SQLite backend -- recreating temporal_metadata.db on the
NFS-backed golden-repos mount (the exact bottleneck Bug #1313 exists to fix).

This module tests the fix: a new env var contract
(CIDX_TEMPORAL_PG_BOOTSTRAP_DIR, see temporal_metadata_backend_registry.py)
that carries ONLY the server's bootstrap dir (never the DSN itself -- argv
and env are world-readable via /proc/<pid>/cmdline/environ) to the child, plus
two functions:

  - build_temporal_child_env(server_config, base_env=None): parent side --
    returns an env dict with the bootstrap-dir var set in postgres mode, or
    None (unchanged env) in sqlite/solo mode.
  - install_postgres_temporal_backend_from_bootstrap(bootstrap_dir): child
    side -- re-reads storage_mode + postgres_dsn from config.json at that
    dir and installs a REAL PostgreSQL-backed factory, or fails LOUD (no
    poison factory in the child -- it is single-purpose and should refuse to
    proceed rather than paper over the misconfiguration).

Live-PG tests are gated by TEST_POSTGRES_DSN (skip when absent), mirroring
the sibling test_temporal_metadata_postgres_backend.py convention.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

try:
    import psycopg  # noqa: F401

    HAS_PSYCOPG = True
except ImportError:
    HAS_PSYCOPG = False


def _postgres_available() -> bool:
    """Cheap boolean availability check for skipif predicates (probe pool
    closed immediately -- mirrors the sibling test file's nit fix so no
    PytestUnraisableExceptionWarning leaks from an unclosed probe pool)."""
    if not HAS_PSYCOPG:
        return False
    dsn = os.environ.get("TEST_POSTGRES_DSN", "")
    if not dsn:
        return False
    try:
        from code_indexer.server.storage.postgres.connection_pool import ConnectionPool

        pool = ConnectionPool(dsn)
        try:
            with pool.connection() as conn:
                conn.execute("SELECT 1")
            return True
        finally:
            pool.close()
    except Exception:
        return False


# ---------------------------------------------------------------------------
# build_temporal_child_env (parent side)
# ---------------------------------------------------------------------------


class TestBuildTemporalChildEnv:
    def test_postgres_mode_returns_dict_with_bootstrap_dir_var(self):
        from code_indexer.server.utils.config_manager import ServerConfig
        from code_indexer.server.storage.postgres.temporal_child_wiring import (
            build_temporal_child_env,
        )
        from code_indexer.storage.temporal_metadata_backend_registry import (
            TEMPORAL_PG_BOOTSTRAP_DIR_ENV,
        )

        server_config = ServerConfig(
            server_dir="/opt/cidx-server",
            storage_mode="postgres",
            postgres_dsn="postgresql://user:pass@host/db",
        )

        result = build_temporal_child_env(server_config, base_env={})

        assert result is not None
        assert result[TEMPORAL_PG_BOOTSTRAP_DIR_ENV] == "/opt/cidx-server"

    def test_postgres_mode_merges_base_env(self):
        from code_indexer.server.utils.config_manager import ServerConfig
        from code_indexer.server.storage.postgres.temporal_child_wiring import (
            build_temporal_child_env,
        )

        server_config = ServerConfig(
            server_dir="/opt/cidx-server",
            storage_mode="postgres",
            postgres_dsn="postgresql://user:pass@host/db",
        )

        result = build_temporal_child_env(
            server_config, base_env={"PATH": "/usr/bin", "OTHER_VAR": "keep-me"}
        )

        assert result is not None
        assert result["PATH"] == "/usr/bin"
        assert result["OTHER_VAR"] == "keep-me"

    def test_sqlite_mode_returns_none(self):
        from code_indexer.server.utils.config_manager import ServerConfig
        from code_indexer.server.storage.postgres.temporal_child_wiring import (
            build_temporal_child_env,
        )

        server_config = ServerConfig(
            server_dir="/opt/cidx-server", storage_mode="sqlite"
        )

        result = build_temporal_child_env(server_config, base_env={})

        assert result is None

    def test_none_config_returns_none(self):
        from code_indexer.server.storage.postgres.temporal_child_wiring import (
            build_temporal_child_env,
        )

        result = build_temporal_child_env(None, base_env={})

        assert result is None

    def test_no_base_env_defaults_to_os_environ_copy(self):
        """When base_env is omitted, the returned dict must still contain
        everything from os.environ (so the child inherits PATH etc.),
        merged with the bootstrap-dir var."""
        from code_indexer.server.utils.config_manager import ServerConfig
        from code_indexer.server.storage.postgres.temporal_child_wiring import (
            build_temporal_child_env,
        )
        from code_indexer.storage.temporal_metadata_backend_registry import (
            TEMPORAL_PG_BOOTSTRAP_DIR_ENV,
        )

        server_config = ServerConfig(
            server_dir="/opt/cidx-server",
            storage_mode="postgres",
            postgres_dsn="postgresql://user:pass@host/db",
        )

        result = build_temporal_child_env(server_config)

        assert result is not None
        assert result[TEMPORAL_PG_BOOTSTRAP_DIR_ENV] == "/opt/cidx-server"
        # Some ambient os.environ key should be present (PATH is set in any
        # sane process environment during test runs).
        assert "PATH" in result


# ---------------------------------------------------------------------------
# install_postgres_temporal_backend_from_bootstrap (child side) -- fail-loud
# unit tests (no real PG required).
# ---------------------------------------------------------------------------


class TestInstallPostgresTemporalBackendFromBootstrapFailLoud:
    def test_sqlite_mode_bootstrap_config_raises(self, tmp_path):
        from code_indexer.server.storage.postgres.temporal_child_wiring import (
            install_postgres_temporal_backend_from_bootstrap,
        )

        bootstrap_dir = tmp_path / "server_dir"
        bootstrap_dir.mkdir()
        (bootstrap_dir / "config.json").write_text(
            json.dumps({"server_dir": str(bootstrap_dir), "storage_mode": "sqlite"})
        )

        with pytest.raises(RuntimeError, match="postgres"):
            install_postgres_temporal_backend_from_bootstrap(str(bootstrap_dir))

    def test_missing_postgres_dsn_raises(self, tmp_path):
        from code_indexer.server.storage.postgres.temporal_child_wiring import (
            install_postgres_temporal_backend_from_bootstrap,
        )

        bootstrap_dir = tmp_path / "server_dir"
        bootstrap_dir.mkdir()
        (bootstrap_dir / "config.json").write_text(
            json.dumps(
                {
                    "server_dir": str(bootstrap_dir),
                    "storage_mode": "postgres",
                    "postgres_dsn": None,
                }
            )
        )

        with pytest.raises(RuntimeError, match="postgres_dsn"):
            install_postgres_temporal_backend_from_bootstrap(str(bootstrap_dir))

    def test_missing_config_file_raises(self, tmp_path):
        from code_indexer.server.storage.postgres.temporal_child_wiring import (
            install_postgres_temporal_backend_from_bootstrap,
        )

        bootstrap_dir = tmp_path / "empty_server_dir"
        bootstrap_dir.mkdir()

        with pytest.raises(RuntimeError):
            install_postgres_temporal_backend_from_bootstrap(str(bootstrap_dir))


# ---------------------------------------------------------------------------
# install_postgres_temporal_backend_from_bootstrap (child side) -- live-PG
# happy path. Gated by TEST_POSTGRES_DSN.
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not _postgres_available(),
    reason="TEST_POSTGRES_DSN not set or PostgreSQL unavailable",
)
class TestInstallPostgresTemporalBackendFromBootstrapLivePg:
    def test_installs_working_factory_with_correct_collection_key(self, tmp_path):
        import hashlib

        from code_indexer.storage.temporal_metadata_backend_registry import (
            clear_temporal_metadata_backend_factory,
            get_temporal_metadata_backend_factory,
        )
        from code_indexer.storage.temporal_metadata_store import COLLECTION_KEY_LENGTH
        from code_indexer.server.storage.postgres.temporal_child_wiring import (
            install_postgres_temporal_backend_from_bootstrap,
        )
        from code_indexer.server.storage.postgres.temporal_metadata_backend import (
            TemporalMetadataPostgresBackend,
        )

        dsn = os.environ["TEST_POSTGRES_DSN"]
        bootstrap_dir = tmp_path / "server_dir"
        bootstrap_dir.mkdir()
        (bootstrap_dir / "config.json").write_text(
            json.dumps(
                {
                    "server_dir": str(bootstrap_dir),
                    "storage_mode": "postgres",
                    "postgres_dsn": dsn,
                }
            )
        )

        pool = None
        try:
            pool = install_postgres_temporal_backend_from_bootstrap(str(bootstrap_dir))
            assert pool is not None

            factory = get_temporal_metadata_backend_factory()
            assert factory is not None

            collection_path = Path("/some/child-process/collection")
            backend = factory(collection_path)

            assert isinstance(backend, TemporalMetadataPostgresBackend)
            expected_key = hashlib.sha256(str(collection_path).encode()).hexdigest()[
                :COLLECTION_KEY_LENGTH
            ]
            assert backend._collection_key == expected_key

            # Real round-trip proves the pool is genuinely usable, not a stub.
            hash_prefixes = backend.save_metadata_batch(
                [("child-proc:test:file.py:0", {"commit_hash": "c1", "path": "f.py"})]
            )
            assert backend.get_point_id(hash_prefixes[0]) == "child-proc:test:file.py:0"
            backend.delete_metadata(hash_prefixes[0])
        finally:
            clear_temporal_metadata_backend_factory()
            if pool is not None:
                pool.close()
