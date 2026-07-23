"""Tests for embedding_stats_config resolution in the cross-process
bootstrap installer (Story #1418 Phase 3).

install_embedding_stats_writer_from_bootstrap() (Phase 2) hardcoded the
writer's flush interval and never honored an "enabled" toggle. This story
adds a READ-ONLY, fail-open resolution of the runtime-DB-only
embedding_stats_config (flush_interval_seconds + enabled), sourced directly
from the sqlite `server_config` table (config_key='runtime') -- NOT via
ConfigService.initialize_runtime_db()/set_connection_pool(), which perform
destructive first-boot migration/seeding side effects (writing to the
runtime DB, stripping config.json) that are NEVER safe to trigger from a
child subprocess's read-only bootstrap path.

When the runtime row/table is absent entirely (today's existing Phase 2
tests, which never seed one), behavior is BYTE IDENTICAL to before this
story: flush_interval defaults to 30.0, enabled defaults to True.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path

import pytest


@pytest.fixture(autouse=True)
def _stop_and_reset_active_writer():
    yield
    from code_indexer.server.services.embedding_stats_writer import (
        EmbeddingStatsWriter,
    )

    writer = EmbeddingStatsWriter._active
    if writer is not None and hasattr(writer, "stop"):
        writer.stop(timeout=2.0)
    EmbeddingStatsWriter._active = None


def _seed_sqlite_runtime_config(db_path: Path, embedding_stats_config: dict) -> None:
    """Write a real server_config runtime row, mirroring
    database_manager.py's CREATE_SERVER_CONFIG_TABLE schema exactly."""
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS server_config (
                config_key TEXT PRIMARY KEY DEFAULT 'runtime',
                config_json TEXT NOT NULL,
                version INTEGER NOT NULL DEFAULT 1,
                updated_at TEXT DEFAULT (datetime('now')),
                updated_by TEXT
            )
            """
        )
        runtime_dict = {"embedding_stats_config": embedding_stats_config}
        conn.execute(
            "INSERT INTO server_config (config_key, config_json, version) "
            "VALUES ('runtime', ?, 1)",
            (json.dumps(runtime_dict),),
        )
        conn.commit()
    finally:
        conn.close()


class TestNoRuntimeRowPreservesPreExistingDefaultBehavior:
    def test_flush_interval_defaults_to_30_when_no_runtime_row(self, tmp_path):
        from code_indexer.server.storage.postgres.embedding_stats_child_wiring import (
            install_embedding_stats_writer_from_bootstrap,
        )

        bootstrap_dir = tmp_path / "server_dir"
        bootstrap_dir.mkdir()
        (bootstrap_dir / "config.json").write_text(
            json.dumps({"server_dir": str(bootstrap_dir), "storage_mode": "sqlite"})
        )

        writer = install_embedding_stats_writer_from_bootstrap(str(bootstrap_dir))

        assert writer._flush_interval_seconds == 30.0

    def test_enabled_defaults_to_true_when_no_runtime_row(self, tmp_path):
        from code_indexer.server.services.embedding_stats_writer import (
            CrossProcessBootstrapWriter,
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


class TestRuntimeRowFlushIntervalIsHonored:
    def test_flush_interval_read_from_runtime_row(self, tmp_path):
        from code_indexer.server.storage.postgres.embedding_stats_child_wiring import (
            install_embedding_stats_writer_from_bootstrap,
        )

        bootstrap_dir = tmp_path / "server_dir"
        bootstrap_dir.mkdir()
        (bootstrap_dir / "config.json").write_text(
            json.dumps({"server_dir": str(bootstrap_dir), "storage_mode": "sqlite"})
        )
        db_path = bootstrap_dir / "data" / "cidx_server.db"
        _seed_sqlite_runtime_config(
            db_path,
            {"enabled": True, "flush_interval_seconds": 5.0, "retention_days": 90},
        )

        writer = install_embedding_stats_writer_from_bootstrap(str(bootstrap_dir))

        assert writer._flush_interval_seconds == 5.0


class TestRuntimeRowEnabledToggleIsHonored:
    def test_disabled_installs_noop_writer_not_cross_process_writer(self, tmp_path):
        from code_indexer.server.services.embedding_stats_writer import NoOpWriter
        from code_indexer.server.storage.postgres.embedding_stats_child_wiring import (
            install_embedding_stats_writer_from_bootstrap,
        )

        bootstrap_dir = tmp_path / "server_dir"
        bootstrap_dir.mkdir()
        (bootstrap_dir / "config.json").write_text(
            json.dumps({"server_dir": str(bootstrap_dir), "storage_mode": "sqlite"})
        )
        db_path = bootstrap_dir / "data" / "cidx_server.db"
        _seed_sqlite_runtime_config(
            db_path,
            {"enabled": False, "flush_interval_seconds": 30.0, "retention_days": 90},
        )

        writer = install_embedding_stats_writer_from_bootstrap(str(bootstrap_dir))

        assert isinstance(writer, NoOpWriter)


class TestResolveEmbeddingStatsConfigDirectUnitCoverage:
    """Direct, function-level tests of _resolve_embedding_stats_config() --
    branches not reachable via install_embedding_stats_writer_from_bootstrap()
    because, by the time that function calls this helper, the sqlite backend
    it just constructed has already created the db file (so a nonexistent
    db_path is unreachable through that path) without a server_config table
    (so a real 'row is None' -- as opposed to a missing-table exception --
    is also unreachable through that path)."""

    def test_nonexistent_db_path_returns_none(self, tmp_path) -> None:
        from code_indexer.server.storage.postgres.embedding_stats_child_wiring import (
            _resolve_embedding_stats_config,
        )

        result = _resolve_embedding_stats_config(
            storage_mode="sqlite",
            db_path=str(tmp_path / "does_not_exist.db"),
            pool=None,
        )

        assert result is None

    def test_table_exists_but_no_runtime_row_returns_none(self, tmp_path) -> None:
        from code_indexer.server.storage.postgres.embedding_stats_child_wiring import (
            _resolve_embedding_stats_config,
        )

        db_path = tmp_path / "stats.db"
        conn = sqlite3.connect(str(db_path))
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS server_config (
                    config_key TEXT PRIMARY KEY DEFAULT 'runtime',
                    config_json TEXT NOT NULL,
                    version INTEGER NOT NULL DEFAULT 1,
                    updated_at TEXT DEFAULT (datetime('now')),
                    updated_by TEXT
                )
                """
            )
            conn.commit()
        finally:
            conn.close()

        result = _resolve_embedding_stats_config(
            storage_mode="sqlite", db_path=str(db_path), pool=None
        )

        assert result is None

    def test_non_dict_embedding_stats_config_value_returns_none(self, tmp_path) -> None:
        from code_indexer.server.storage.postgres.embedding_stats_child_wiring import (
            _resolve_embedding_stats_config,
        )

        db_path = tmp_path / "stats.db"
        _seed_sqlite_runtime_config(db_path, "not-a-dict")  # type: ignore[arg-type]

        result = _resolve_embedding_stats_config(
            storage_mode="sqlite", db_path=str(db_path), pool=None
        )

        assert result is None


if __name__ == "__main__":
    import pytest as _pytest

    _pytest.main([__file__, "-v"])
