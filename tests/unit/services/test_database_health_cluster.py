"""
Unit tests for Story #505: Storage-Mode-Aware DatabaseHealthService.

Tests that the service correctly adapts its database list based on storage_mode:
- SQLite mode: returns all 8 original databases (backward compatibility)
- Postgres mode: skips migrated databases, adds PostgreSQL connectivity check
- Postgres mode: retains local-only databases (oauth, refresh_tokens, scip_audit)
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.services.database_health_service import (
    DATABASE_DISPLAY_NAMES,
    POSTGRES_MIGRATED_DATABASES,
    DatabaseHealthService,
    DatabaseHealthStatus,
    _reset_singleton_for_testing,
    get_database_health_service,
)


@pytest.fixture(autouse=True)
def reset_singleton():
    """Reset the singleton before each test to ensure isolation."""
    _reset_singleton_for_testing()
    yield
    _reset_singleton_for_testing()


def _make_mock_health_result(db_path, display_name="Unknown"):
    """Helper: return a MagicMock DatabaseHealthResult for a given db_path."""
    mock = MagicMock()
    mock.file_name = Path(db_path).name
    mock.display_name = display_name
    mock.status = DatabaseHealthStatus.ERROR
    mock.checks = {}
    mock.db_path = db_path
    return mock


class TestStandaloneModeReturns8Databases:
    """SQLite (standalone) mode must return all 8 databases unchanged."""

    def test_sqlite_mode_returns_all_8_databases(self, tmp_path):
        """
        Story #505: In sqlite mode the service must return exactly 8 results,
        one per entry in DATABASE_DISPLAY_NAMES, with no PostgreSQL entry.
        """
        service = DatabaseHealthService(
            server_dir=str(tmp_path),
            storage_mode="sqlite",
        )

        with patch.object(
            DatabaseHealthService,
            "check_database_health",
            side_effect=_make_mock_health_result,
        ):
            results = service.get_all_database_health()

        assert len(results) == len(DATABASE_DISPLAY_NAMES)
        file_names = {r.file_name for r in results}
        assert file_names == set(DATABASE_DISPLAY_NAMES.keys())
        assert "postgresql" not in file_names

    def test_sqlite_mode_cached_returns_all_8_databases(self, tmp_path):
        """
        Story #505: Cached variant also returns 8 databases in sqlite mode.
        """
        service = DatabaseHealthService(
            server_dir=str(tmp_path),
            storage_mode="sqlite",
        )

        with patch.object(
            DatabaseHealthService,
            "check_database_health",
            side_effect=_make_mock_health_result,
        ):
            results = service.get_all_database_health_cached()

        assert len(results) == len(DATABASE_DISPLAY_NAMES)
        assert "postgresql" not in {r.file_name for r in results}


class TestPostgresModeSkipsMigratedDatabases:
    """In postgres mode, POSTGRES_MIGRATED_DATABASES must not appear in results."""

    def test_postgres_mode_omits_migrated_databases(self, tmp_path):
        """
        Story #505: Migrated databases (cidx_server, logs, api_metrics,
        payload_cache, groups) must be absent from postgres mode results.
        """
        service = DatabaseHealthService(
            server_dir=str(tmp_path),
            storage_mode="postgres",
            postgres_dsn=None,
        )

        with patch.object(
            DatabaseHealthService,
            "check_database_health",
            side_effect=_make_mock_health_result,
        ):
            results = service.get_all_database_health()

        result_file_names = {r.file_name for r in results}

        for migrated_db in POSTGRES_MIGRATED_DATABASES:
            assert (
                migrated_db not in result_file_names
            ), f"Migrated database '{migrated_db}' should not appear in postgres mode"

    def test_postgres_mode_total_count_is_reduced(self, tmp_path):
        """
        Story #505: Postgres mode returns fewer results than sqlite mode
        (migrated DBs replaced by 1 PostgreSQL entry).
        """
        sqlite_service = DatabaseHealthService(
            server_dir=str(tmp_path), storage_mode="sqlite"
        )
        pg_service = DatabaseHealthService(
            server_dir=str(tmp_path), storage_mode="postgres", postgres_dsn=None
        )

        with patch.object(
            DatabaseHealthService,
            "check_database_health",
            side_effect=_make_mock_health_result,
        ):
            sqlite_results = sqlite_service.get_all_database_health()
            pg_results = pg_service.get_all_database_health()

        expected_pg_count = (
            len(DATABASE_DISPLAY_NAMES) - len(POSTGRES_MIGRATED_DATABASES) + 1
        )
        assert len(pg_results) == expected_pg_count
        assert len(sqlite_results) == len(DATABASE_DISPLAY_NAMES)


class TestPostgresModeIncludesPostgreSQLHealth:
    """Postgres mode must include a PostgreSQL connectivity entry."""

    def test_postgres_mode_has_postgresql_entry(self, tmp_path):
        """
        Story #505: Results in postgres mode must include one entry with
        file_name == "postgresql" and display_name == "PostgreSQL".
        """
        service = DatabaseHealthService(
            server_dir=str(tmp_path),
            storage_mode="postgres",
            postgres_dsn=None,
        )

        with patch.object(
            DatabaseHealthService,
            "check_database_health",
            side_effect=_make_mock_health_result,
        ):
            results = service.get_all_database_health()

        pg_entries = [r for r in results if r.file_name == "postgresql"]
        assert len(pg_entries) == 1
        assert pg_entries[0].display_name == "PostgreSQL"

    def test_postgresql_entry_is_error_when_no_dsn(self, tmp_path):
        """
        Story #505: When postgres_dsn is None, PostgreSQL entry has ERROR status.
        """
        service = DatabaseHealthService(
            server_dir=str(tmp_path),
            storage_mode="postgres",
            postgres_dsn=None,
        )

        with patch.object(
            DatabaseHealthService,
            "check_database_health",
            side_effect=_make_mock_health_result,
        ):
            results = service.get_all_database_health()

        pg_entry = next(r for r in results if r.file_name == "postgresql")
        assert pg_entry.status == DatabaseHealthStatus.ERROR
        assert not pg_entry.checks["connect"].passed
        assert "No postgres_dsn" in (pg_entry.checks["connect"].error_message or "")

    def test_postgresql_entry_is_first_in_list(self, tmp_path):
        """
        Story #505: PostgreSQL entry appears first in the results list
        so it is the most prominent entry in the honeycomb.
        """
        service = DatabaseHealthService(
            server_dir=str(tmp_path),
            storage_mode="postgres",
            postgres_dsn=None,
        )

        with patch.object(
            DatabaseHealthService,
            "check_database_health",
            side_effect=_make_mock_health_result,
        ):
            results = service.get_all_database_health()

        assert results[0].file_name == "postgresql"

    def test_postgresql_entry_healthy_when_connection_succeeds(self, tmp_path):
        """
        Story #505: When psycopg.connect() succeeds, PostgreSQL entry is HEALTHY.
        """
        service = DatabaseHealthService(
            server_dir=str(tmp_path),
            storage_mode="postgres",
            postgres_dsn="postgresql://user:pass@localhost/db",
        )

        mock_conn = MagicMock()
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)
        mock_psycopg = MagicMock()
        mock_psycopg.connect.return_value = mock_conn

        with patch.dict("sys.modules", {"psycopg": mock_psycopg}):
            pg_result = service._check_postgresql_health()

        assert pg_result.status == DatabaseHealthStatus.HEALTHY
        assert pg_result.checks["connect"].passed
        assert pg_result.checks["read"].passed


class TestPostgresModeKeepsLocalOnlyDatabases:
    """
    Databases not in POSTGRES_MIGRATED_DATABASES must remain in postgres mode results.

    Local-only databases: oauth.db, refresh_tokens.db, scip_audit.db
    """

    def test_local_only_databases_present_in_postgres_mode(self, tmp_path):
        """
        Story #505: oauth.db, refresh_tokens.db and scip_audit.db are
        NOT migrated to PostgreSQL and must still appear in postgres mode.
        """
        local_only_dbs = (
            set(DATABASE_DISPLAY_NAMES.keys()) - POSTGRES_MIGRATED_DATABASES
        )
        assert local_only_dbs, "There should be local-only databases"

        service = DatabaseHealthService(
            server_dir=str(tmp_path),
            storage_mode="postgres",
            postgres_dsn=None,
        )

        with patch.object(
            DatabaseHealthService,
            "check_database_health",
            side_effect=_make_mock_health_result,
        ):
            results = service.get_all_database_health()

        result_file_names = {r.file_name for r in results}

        for local_db in local_only_dbs:
            assert (
                local_db in result_file_names
            ), f"Local-only database '{local_db}' must remain in postgres mode results"

    def test_oauth_db_is_local_only(self):
        """oauth.db is a local-only database - not in POSTGRES_MIGRATED_DATABASES."""
        assert "oauth.db" not in POSTGRES_MIGRATED_DATABASES

    def test_refresh_tokens_db_is_local_only(self):
        """refresh_tokens.db is local-only - not in POSTGRES_MIGRATED_DATABASES."""
        assert "refresh_tokens.db" not in POSTGRES_MIGRATED_DATABASES

    def test_scip_audit_db_is_local_only(self):
        """scip_audit.db is local-only - not in POSTGRES_MIGRATED_DATABASES."""
        assert "scip_audit.db" not in POSTGRES_MIGRATED_DATABASES


class TestSingletonStorageMode:
    """Singleton get_database_health_service() must respect storage_mode on first call."""

    def test_singleton_created_with_sqlite_mode_by_default(self, tmp_path):
        """Default mode is sqlite."""
        service = get_database_health_service(server_dir=str(tmp_path))
        assert service.storage_mode == "sqlite"
        assert service.postgres_dsn is None

    def test_singleton_created_with_postgres_mode(self, tmp_path):
        """First call with postgres mode stores mode in singleton."""
        dsn = "postgresql://user:pass@localhost/db"
        service = get_database_health_service(
            server_dir=str(tmp_path),
            storage_mode="postgres",
            postgres_dsn=dsn,
        )
        assert service.storage_mode == "postgres"
        assert service.postgres_dsn == dsn

    def test_singleton_ignores_subsequent_calls(self, tmp_path):
        """Singleton returns the same instance regardless of subsequent parameters."""
        first = get_database_health_service(
            server_dir=str(tmp_path), storage_mode="sqlite"
        )
        second = get_database_health_service(
            server_dir=str(tmp_path), storage_mode="postgres"
        )
        assert first is second
        assert second.storage_mode == "sqlite"
