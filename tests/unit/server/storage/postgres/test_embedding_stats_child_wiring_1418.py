"""Story #1418 Phase 2 of 3: cross-process embedding-stats bootstrap wiring.

Mirrors the Bug #1313 `temporal_child_wiring.py` parent/child env-var IPC
pattern, with one critical difference: unlike
``build_temporal_child_env`` (fires ONLY in postgres/cluster mode --
storage-mode discovery is not the problem there, the PG factory needs to
cross a process boundary), ``build_embedding_stats_child_env`` fires
UNCONDITIONALLY for BOTH SQLite solo AND PostgreSQL cluster storage modes:
the child subprocess has no other way to discover the server's data
directory at all (a pure discovery problem, not a backend-crossing-a-
process-boundary problem).

  - build_embedding_stats_child_env(server_config, base_env=None): PARENT
    side -- always returns a dict with CIDX_EMBEDDING_STATS_BOOTSTRAP_DIR
    set to server_config.server_dir, unless server_config is None (no
    server context available -- child falls back to NoOpWriter, matching
    standalone CLI behavior).
  - install_embedding_stats_writer_from_bootstrap(bootstrap_dir): CHILD
    side -- re-reads storage_mode + (for postgres) postgres_dsn from
    config.json at bootstrap_dir, resolves the matching
    EmbeddingCallStats*Backend, constructs+starts a
    CrossProcessBootstrapWriter, and installs it via
    EmbeddingStatsWriter.set_active(). Unlike temporal's child-side
    installer, this is fail-OPEN, not fail-loud: a misconfigured/missing
    bootstrap must never abort real indexing work over a stats side
    channel (CLAUDE.md fail-open convention for observability data) --
    on any resolution failure this function falls back to installing a
    NoOpWriter and logs a WARNING, rather than raising.
"""

from __future__ import annotations

import json
import os

import pytest


# ---------------------------------------------------------------------------
# build_embedding_stats_child_env (parent side)
# ---------------------------------------------------------------------------


class TestBuildEmbeddingStatsChildEnvSqliteMode:
    def test_sqlite_mode_returns_dict_with_bootstrap_dir_var(self):
        from code_indexer.server.utils.config_manager import ServerConfig
        from code_indexer.server.storage.postgres.embedding_stats_child_wiring import (
            build_embedding_stats_child_env,
            EMBEDDING_STATS_BOOTSTRAP_DIR_ENV,
        )

        server_config = ServerConfig(
            server_dir="/opt/cidx-server", storage_mode="sqlite"
        )

        result = build_embedding_stats_child_env(server_config, base_env={})

        assert result[EMBEDDING_STATS_BOOTSTRAP_DIR_ENV] == "/opt/cidx-server"


class TestBuildEmbeddingStatsChildEnvPostgresMode:
    def test_postgres_mode_returns_dict_with_bootstrap_dir_var(self):
        from code_indexer.server.utils.config_manager import ServerConfig
        from code_indexer.server.storage.postgres.embedding_stats_child_wiring import (
            build_embedding_stats_child_env,
            EMBEDDING_STATS_BOOTSTRAP_DIR_ENV,
        )

        server_config = ServerConfig(
            server_dir="/opt/cidx-server",
            storage_mode="postgres",
            postgres_dsn="postgresql://user:pass@host/db",
        )

        result = build_embedding_stats_child_env(server_config, base_env={})

        assert result[EMBEDDING_STATS_BOOTSTRAP_DIR_ENV] == "/opt/cidx-server"

    def test_merges_base_env_without_dropping_existing_keys(self):
        from code_indexer.server.utils.config_manager import ServerConfig
        from code_indexer.server.storage.postgres.embedding_stats_child_wiring import (
            build_embedding_stats_child_env,
        )

        server_config = ServerConfig(
            server_dir="/opt/cidx-server",
            storage_mode="postgres",
            postgres_dsn="postgresql://user:pass@host/db",
        )

        result = build_embedding_stats_child_env(
            server_config, base_env={"PATH": "/usr/bin", "OTHER_VAR": "keep-me"}
        )

        assert result["PATH"] == "/usr/bin"
        assert result["OTHER_VAR"] == "keep-me"

    def test_does_not_mutate_input_base_env(self):
        from code_indexer.server.utils.config_manager import ServerConfig
        from code_indexer.server.storage.postgres.embedding_stats_child_wiring import (
            build_embedding_stats_child_env,
        )

        server_config = ServerConfig(server_dir="/opt/cidx-server")
        original = {"PATH": "/usr/bin"}
        build_embedding_stats_child_env(server_config, base_env=original)

        assert original == {"PATH": "/usr/bin"}


class TestBuildEmbeddingStatsChildEnvNoneConfig:
    def test_none_config_returns_base_env_unchanged(self):
        from code_indexer.server.storage.postgres.embedding_stats_child_wiring import (
            build_embedding_stats_child_env,
            EMBEDDING_STATS_BOOTSTRAP_DIR_ENV,
        )

        result = build_embedding_stats_child_env(None, base_env={"PATH": "/usr/bin"})

        assert result == {"PATH": "/usr/bin"}
        assert EMBEDDING_STATS_BOOTSTRAP_DIR_ENV not in result

    def test_no_base_env_defaults_to_os_environ_copy(self):
        from code_indexer.server.utils.config_manager import ServerConfig
        from code_indexer.server.storage.postgres.embedding_stats_child_wiring import (
            build_embedding_stats_child_env,
            EMBEDDING_STATS_BOOTSTRAP_DIR_ENV,
        )

        server_config = ServerConfig(server_dir="/opt/cidx-server")
        result = build_embedding_stats_child_env(server_config)

        assert result[EMBEDDING_STATS_BOOTSTRAP_DIR_ENV] == "/opt/cidx-server"
        assert "PATH" in result  # ambient os.environ key present


# ---------------------------------------------------------------------------
# install_embedding_stats_writer_from_bootstrap (child side) -- fail-OPEN
# unlike temporal's fail-loud child installer.
# ---------------------------------------------------------------------------


class TestInstallEmbeddingStatsWriterFromBootstrapSqlite:
    def teardown_method(self):
        from code_indexer.server.services.embedding_stats_writer import (
            EmbeddingStatsWriter,
        )

        writer = EmbeddingStatsWriter._active
        if writer is not None and hasattr(writer, "stop"):
            writer.stop(timeout=2.0)
        EmbeddingStatsWriter._active = None

    def test_sqlite_mode_installs_started_cross_process_writer(self, tmp_path):
        from code_indexer.server.services.embedding_stats_writer import (
            CrossProcessBootstrapWriter,
            EmbeddingStatsWriter,
        )
        from code_indexer.server.storage.postgres.embedding_stats_child_wiring import (
            install_embedding_stats_writer_from_bootstrap,
        )

        bootstrap_dir = tmp_path / "server_dir"
        bootstrap_dir.mkdir()
        (bootstrap_dir / "config.json").write_text(
            json.dumps({"server_dir": str(bootstrap_dir), "storage_mode": "sqlite"})
        )

        writer = install_embedding_stats_writer_from_bootstrap(str(bootstrap_dir))

        assert isinstance(writer, CrossProcessBootstrapWriter)
        assert EmbeddingStatsWriter.get_active() is writer
        assert writer._thread is not None and writer._thread.is_alive()

    def test_sqlite_mode_creates_the_db_file(self, tmp_path):
        from code_indexer.server.storage.postgres.embedding_stats_child_wiring import (
            install_embedding_stats_writer_from_bootstrap,
        )

        bootstrap_dir = tmp_path / "server_dir"
        bootstrap_dir.mkdir()
        (bootstrap_dir / "config.json").write_text(
            json.dumps({"server_dir": str(bootstrap_dir), "storage_mode": "sqlite"})
        )

        install_embedding_stats_writer_from_bootstrap(str(bootstrap_dir))

        assert (bootstrap_dir / "data" / "cidx_server.db").exists()


class TestInstallEmbeddingStatsWriterFromBootstrapFailOpen:
    """Unlike temporal's fail-loud child installer, this MUST NEVER raise --
    a stats side-channel failure must never abort real indexing work."""

    def teardown_method(self):
        from code_indexer.server.services.embedding_stats_writer import (
            EmbeddingStatsWriter,
        )

        writer = EmbeddingStatsWriter._active
        if writer is not None and hasattr(writer, "stop"):
            writer.stop(timeout=2.0)
        EmbeddingStatsWriter._active = None

    def test_missing_config_file_installs_noop_writer_without_raising(self, tmp_path):
        from code_indexer.server.services.embedding_stats_writer import (
            EmbeddingStatsWriter,
            NoOpWriter,
        )
        from code_indexer.server.storage.postgres.embedding_stats_child_wiring import (
            install_embedding_stats_writer_from_bootstrap,
        )

        bootstrap_dir = tmp_path / "empty_server_dir"
        bootstrap_dir.mkdir()

        writer = install_embedding_stats_writer_from_bootstrap(str(bootstrap_dir))

        assert isinstance(writer, NoOpWriter)
        assert isinstance(EmbeddingStatsWriter.get_active(), NoOpWriter)

    def test_missing_postgres_dsn_installs_noop_writer_without_raising(self, tmp_path):
        from code_indexer.server.services.embedding_stats_writer import NoOpWriter
        from code_indexer.server.storage.postgres.embedding_stats_child_wiring import (
            install_embedding_stats_writer_from_bootstrap,
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

        writer = install_embedding_stats_writer_from_bootstrap(str(bootstrap_dir))

        assert isinstance(writer, NoOpWriter)


# ---------------------------------------------------------------------------
# install_embedding_stats_writer_from_bootstrap (child side) -- live-PG
# happy path. Gated by TEST_POSTGRES_DSN.
# ---------------------------------------------------------------------------


def _postgres_available() -> bool:
    try:
        import psycopg  # noqa: F401
    except ImportError:
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


@pytest.mark.skipif(
    not _postgres_available(),
    reason="TEST_POSTGRES_DSN not set or PostgreSQL unavailable",
)
class TestInstallEmbeddingStatsWriterFromBootstrapLivePg:
    def teardown_method(self):
        from code_indexer.server.services.embedding_stats_writer import (
            EmbeddingStatsWriter,
        )

        writer = EmbeddingStatsWriter._active
        if writer is not None and hasattr(writer, "stop"):
            writer.stop(timeout=2.0)
        EmbeddingStatsWriter._active = None

    def test_postgres_mode_installs_writer_that_persists_a_real_row(self, tmp_path):
        import time

        from code_indexer.server.services.embedding_call_stats import (
            EmbeddingCallRecord,
        )
        from code_indexer.server.services.embedding_stats_writer import (
            CrossProcessBootstrapWriter,
            EmbeddingStatsWriter,
        )
        from code_indexer.server.storage.postgres.embedding_stats_child_wiring import (
            install_embedding_stats_writer_from_bootstrap,
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

        writer = install_embedding_stats_writer_from_bootstrap(str(bootstrap_dir))
        assert isinstance(writer, CrossProcessBootstrapWriter)

        record = EmbeddingCallRecord(
            provider="voyageai",
            call_type="embed",
            model="voyage-code-3",
            item_count=1,
            token_count=5,
            batch_size=1,
            purpose="index",
            success=True,
            latency_ms=10,
            occurred_at=time.time(),
        )
        EmbeddingStatsWriter.get_active().record(record)
        writer.flush()

        rows = writer._backend.query(limit=10)
        assert any(r.model == "voyage-code-3" for r in rows)
