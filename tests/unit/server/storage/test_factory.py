"""
Unit tests for StorageFactory and BackendRegistry (Story #417).

Covers:
- SQLite mode: no storage_mode key in config (backward compat)
- SQLite mode: explicit storage_mode="sqlite"
- BackendRegistry has all required fields
- All SQLite backend fields satisfy their Protocol types
- postgres mode: lazy import attempted (mocked)
- Invalid storage_mode raises ValueError
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _init_db(tmp_path: Path) -> str:
    """Initialise main cidx_server.db and return data_dir string."""
    from code_indexer.server.storage.database_manager import DatabaseSchema

    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = str(data_dir / "cidx_server.db")
    DatabaseSchema(db_path).initialize_database()
    return str(data_dir)


# ---------------------------------------------------------------------------
# BackendRegistry structure
# ---------------------------------------------------------------------------


class TestBackendRegistryStructure:
    """BackendRegistry must expose all 15 required fields (including node_metrics)."""

    REQUIRED_FIELDS = {
        "global_repos",
        "users",
        "sessions",
        "background_jobs",
        "sync_jobs",
        "ci_tokens",
        "description_refresh_tracking",
        "ssh_keys",
        "golden_repo_metadata",
        "dependency_map_tracking",
        "git_credentials",
        "repo_category",
        "groups",
        "audit_log",
        "node_metrics",
    }

    def test_backend_registry_has_all_required_fields(self) -> None:
        """BackendRegistry dataclass must declare all 15 required fields including node_metrics."""
        from code_indexer.server.storage.factory import BackendRegistry

        field_names = {f.name for f in dataclasses.fields(BackendRegistry)}
        missing = self.REQUIRED_FIELDS - field_names
        assert not missing, f"BackendRegistry is missing fields: {missing}"

    def test_backend_registry_has_node_metrics_field(self) -> None:
        """BackendRegistry must have a node_metrics field for cluster dashboard (Story #492)."""
        from code_indexer.server.storage.factory import BackendRegistry

        field_names = {f.name for f in dataclasses.fields(BackendRegistry)}
        assert (
            "node_metrics" in field_names
        ), "BackendRegistry.node_metrics is required for cluster-aware dashboard"

    def test_backend_registry_is_dataclass(self) -> None:
        """BackendRegistry must be a dataclass."""
        from code_indexer.server.storage.factory import BackendRegistry

        assert dataclasses.is_dataclass(BackendRegistry)


# ---------------------------------------------------------------------------
# node_metrics backend in StorageFactory
# ---------------------------------------------------------------------------


class TestStorageFactoryNodeMetrics:
    """StorageFactory must create NodeMetricsBackend in SQLite mode."""

    def test_sqlite_mode_creates_node_metrics_backend(self, tmp_path: Path) -> None:
        """StorageFactory SQLite mode must create NodeMetricsSqliteBackend satisfying NodeMetricsBackend Protocol."""
        from code_indexer.server.storage.factory import StorageFactory
        from code_indexer.server.storage.protocols import NodeMetricsBackend
        from code_indexer.server.storage.sqlite_backends import NodeMetricsSqliteBackend

        data_dir = _init_db(tmp_path)
        registry = StorageFactory.create_backends(config={}, data_dir=data_dir)

        assert registry.node_metrics is not None, "node_metrics must not be None"
        assert isinstance(
            registry.node_metrics, NodeMetricsSqliteBackend
        ), f"Expected NodeMetricsSqliteBackend, got {type(registry.node_metrics)}"
        assert isinstance(
            registry.node_metrics, NodeMetricsBackend
        ), "node_metrics must satisfy NodeMetricsBackend Protocol"


# ---------------------------------------------------------------------------
# SQLite mode — no storage_mode key (backward compat)
# ---------------------------------------------------------------------------


class TestSQLiteModeNoKey:
    """When config has no storage_mode key, factory must use SQLite."""

    def test_returns_backend_registry(self, tmp_path: Path) -> None:
        from code_indexer.server.storage.factory import BackendRegistry, StorageFactory

        data_dir = _init_db(tmp_path)
        result = StorageFactory.create_backends(config={}, data_dir=data_dir)
        assert isinstance(result, BackendRegistry)

    def test_all_fields_populated(self, tmp_path: Path) -> None:
        from code_indexer.server.storage.factory import StorageFactory

        data_dir = _init_db(tmp_path)
        registry = StorageFactory.create_backends(config={}, data_dir=data_dir)

        for field in dataclasses.fields(registry):
            # Skip optional fields that are intentionally None in SQLite mode
            # (e.g. connection_pool, which is only set in PostgreSQL mode).
            if field.default is None:
                continue
            value = getattr(registry, field.name)
            assert value is not None, f"Field {field.name!r} is None"


# ---------------------------------------------------------------------------
# SQLite mode — explicit storage_mode="sqlite"
# ---------------------------------------------------------------------------


class TestSQLiteModeExplicit:
    """When config has storage_mode="sqlite", factory must use SQLite."""

    def test_returns_backend_registry(self, tmp_path: Path) -> None:
        from code_indexer.server.storage.factory import BackendRegistry, StorageFactory

        data_dir = _init_db(tmp_path)
        result = StorageFactory.create_backends(
            config={"storage_mode": "sqlite"}, data_dir=data_dir
        )
        assert isinstance(result, BackendRegistry)

    def test_sqlite_backends_no_psycopg_needed(self, tmp_path: Path) -> None:
        """SQLite mode must not import psycopg at all."""
        import sys

        data_dir = _init_db(tmp_path)

        # Remove psycopg from sys.modules to simulate it not being installed
        psycopg_keys = [k for k in sys.modules if k.startswith("psycopg")]
        saved = {k: sys.modules.pop(k) for k in psycopg_keys}
        try:
            from code_indexer.server.storage.factory import StorageFactory

            registry = StorageFactory.create_backends(
                config={"storage_mode": "sqlite"}, data_dir=data_dir
            )
            assert registry is not None
        finally:
            sys.modules.update(saved)


# ---------------------------------------------------------------------------
# SQLite backends satisfy their Protocol types
# ---------------------------------------------------------------------------


class TestSQLiteProtocolSatisfaction:
    """Every field in the registry must satisfy its Protocol type."""

    def test_global_repos_satisfies_protocol(self, tmp_path: Path) -> None:
        from code_indexer.server.storage.factory import StorageFactory
        from code_indexer.server.storage.protocols import GlobalReposBackend

        data_dir = _init_db(tmp_path)
        registry = StorageFactory.create_backends(config={}, data_dir=data_dir)
        assert isinstance(registry.global_repos, GlobalReposBackend)

    def test_users_satisfies_protocol(self, tmp_path: Path) -> None:
        from code_indexer.server.storage.factory import StorageFactory
        from code_indexer.server.storage.protocols import UsersBackend

        data_dir = _init_db(tmp_path)
        registry = StorageFactory.create_backends(config={}, data_dir=data_dir)
        assert isinstance(registry.users, UsersBackend)

    def test_sessions_satisfies_protocol(self, tmp_path: Path) -> None:
        from code_indexer.server.storage.factory import StorageFactory
        from code_indexer.server.storage.protocols import SessionsBackend

        data_dir = _init_db(tmp_path)
        registry = StorageFactory.create_backends(config={}, data_dir=data_dir)
        assert isinstance(registry.sessions, SessionsBackend)

    def test_background_jobs_satisfies_protocol(self, tmp_path: Path) -> None:
        from code_indexer.server.storage.factory import StorageFactory
        from code_indexer.server.storage.protocols import BackgroundJobsBackend

        data_dir = _init_db(tmp_path)
        registry = StorageFactory.create_backends(config={}, data_dir=data_dir)
        assert isinstance(registry.background_jobs, BackgroundJobsBackend)

    def test_sync_jobs_satisfies_protocol(self, tmp_path: Path) -> None:
        from code_indexer.server.storage.factory import StorageFactory
        from code_indexer.server.storage.protocols import SyncJobsBackend

        data_dir = _init_db(tmp_path)
        registry = StorageFactory.create_backends(config={}, data_dir=data_dir)
        assert isinstance(registry.sync_jobs, SyncJobsBackend)

    def test_ci_tokens_satisfies_protocol(self, tmp_path: Path) -> None:
        from code_indexer.server.storage.factory import StorageFactory
        from code_indexer.server.storage.protocols import CITokensBackend

        data_dir = _init_db(tmp_path)
        registry = StorageFactory.create_backends(config={}, data_dir=data_dir)
        assert isinstance(registry.ci_tokens, CITokensBackend)

    def test_description_refresh_tracking_satisfies_protocol(
        self, tmp_path: Path
    ) -> None:
        from code_indexer.server.storage.factory import StorageFactory
        from code_indexer.server.storage.protocols import (
            DescriptionRefreshTrackingBackend,
        )

        data_dir = _init_db(tmp_path)
        registry = StorageFactory.create_backends(config={}, data_dir=data_dir)
        assert isinstance(
            registry.description_refresh_tracking, DescriptionRefreshTrackingBackend
        )

    def test_ssh_keys_satisfies_protocol(self, tmp_path: Path) -> None:
        from code_indexer.server.storage.factory import StorageFactory
        from code_indexer.server.storage.protocols import SSHKeysBackend

        data_dir = _init_db(tmp_path)
        registry = StorageFactory.create_backends(config={}, data_dir=data_dir)
        assert isinstance(registry.ssh_keys, SSHKeysBackend)

    def test_golden_repo_metadata_satisfies_protocol(self, tmp_path: Path) -> None:
        from code_indexer.server.storage.factory import StorageFactory
        from code_indexer.server.storage.protocols import GoldenRepoMetadataBackend

        data_dir = _init_db(tmp_path)
        registry = StorageFactory.create_backends(config={}, data_dir=data_dir)
        assert isinstance(registry.golden_repo_metadata, GoldenRepoMetadataBackend)

    def test_dependency_map_tracking_satisfies_protocol(self, tmp_path: Path) -> None:
        from code_indexer.server.storage.factory import StorageFactory
        from code_indexer.server.storage.protocols import DependencyMapTrackingBackend

        data_dir = _init_db(tmp_path)
        registry = StorageFactory.create_backends(config={}, data_dir=data_dir)
        assert isinstance(
            registry.dependency_map_tracking, DependencyMapTrackingBackend
        )

    def test_git_credentials_satisfies_protocol(self, tmp_path: Path) -> None:
        from code_indexer.server.storage.factory import StorageFactory
        from code_indexer.server.storage.protocols import GitCredentialsBackend

        data_dir = _init_db(tmp_path)
        registry = StorageFactory.create_backends(config={}, data_dir=data_dir)
        assert isinstance(registry.git_credentials, GitCredentialsBackend)

    def test_repo_category_satisfies_protocol(self, tmp_path: Path) -> None:
        from code_indexer.server.storage.factory import StorageFactory
        from code_indexer.server.storage.protocols import RepoCategoryBackend

        data_dir = _init_db(tmp_path)
        registry = StorageFactory.create_backends(config={}, data_dir=data_dir)
        assert isinstance(registry.repo_category, RepoCategoryBackend)

    def test_groups_satisfies_protocol(self, tmp_path: Path) -> None:
        from code_indexer.server.storage.factory import StorageFactory
        from code_indexer.server.storage.protocols import GroupsBackend

        data_dir = _init_db(tmp_path)
        registry = StorageFactory.create_backends(config={}, data_dir=data_dir)
        assert isinstance(registry.groups, GroupsBackend)

    def test_audit_log_satisfies_protocol(self, tmp_path: Path) -> None:
        from code_indexer.server.storage.factory import StorageFactory
        from code_indexer.server.storage.protocols import AuditLogBackend

        data_dir = _init_db(tmp_path)
        registry = StorageFactory.create_backends(config={}, data_dir=data_dir)
        assert isinstance(registry.audit_log, AuditLogBackend)


# ---------------------------------------------------------------------------
# PostgreSQL mode — lazy import tested with mock
# ---------------------------------------------------------------------------


class TestPostgresModeImport:
    """Postgres mode must attempt lazy import of psycopg / ConnectionPool."""

    def test_postgres_mode_raises_import_error_without_psycopg(self) -> None:
        """When psycopg is not installed, postgres mode must raise ImportError."""

        # Patch ConnectionPool to raise ImportError (simulates missing psycopg)
        with patch(
            "code_indexer.server.storage.factory.StorageFactory._create_postgres_backends"
        ) as mock_pg:
            mock_pg.side_effect = ImportError("psycopg (v3) is required")

            from code_indexer.server.storage.factory import StorageFactory

            with pytest.raises(ImportError, match="psycopg"):
                StorageFactory.create_backends(
                    config={
                        "storage_mode": "postgres",
                        "postgres_dsn": "postgresql://x",
                    },
                    data_dir="/tmp/unused",
                )

    def test_postgres_mode_calls_create_postgres_backends(self) -> None:
        """create_backends() with postgres mode must delegate to _create_postgres_backends."""
        fake_registry = MagicMock()

        with patch(
            "code_indexer.server.storage.factory.StorageFactory._create_postgres_backends",
            return_value=fake_registry,
        ) as mock_pg:
            from code_indexer.server.storage.factory import StorageFactory

            result = StorageFactory.create_backends(
                config={"storage_mode": "postgres", "postgres_dsn": "postgresql://x"},
                data_dir="/tmp/unused",
            )

        mock_pg.assert_called_once_with(
            {"storage_mode": "postgres", "postgres_dsn": "postgresql://x"}
        )
        assert result is fake_registry

    def test_postgres_mode_does_not_call_sqlite_path(self) -> None:
        """When postgres mode selected, SQLite path must NOT be called."""
        fake_registry = MagicMock()

        with (
            patch(
                "code_indexer.server.storage.factory.StorageFactory._create_postgres_backends",
                return_value=fake_registry,
            ),
            patch(
                "code_indexer.server.storage.factory.StorageFactory._create_sqlite_backends"
            ) as mock_sqlite,
        ):
            from code_indexer.server.storage.factory import StorageFactory

            StorageFactory.create_backends(
                config={"storage_mode": "postgres", "postgres_dsn": "postgresql://x"},
                data_dir="/tmp/unused",
            )

        mock_sqlite.assert_not_called()


# ---------------------------------------------------------------------------
# Invalid storage_mode
# ---------------------------------------------------------------------------


class TestInvalidMode:
    def test_unknown_storage_mode_raises_value_error(self, tmp_path: Path) -> None:
        from code_indexer.server.storage.factory import StorageFactory

        with pytest.raises(ValueError, match="Unsupported storage_mode"):
            StorageFactory.create_backends(
                config={"storage_mode": "redis"},
                data_dir=str(tmp_path),
            )

    def test_error_message_includes_mode_name(self, tmp_path: Path) -> None:
        from code_indexer.server.storage.factory import StorageFactory

        with pytest.raises(ValueError, match="redis"):
            StorageFactory.create_backends(
                config={"storage_mode": "redis"},
                data_dir=str(tmp_path),
            )
