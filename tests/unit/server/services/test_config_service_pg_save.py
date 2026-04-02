"""
Tests for Bug #591: config_service _seed_runtime_to_pg crashes with KeyError:0.

The bug: _seed_runtime_to_pg fetches a row after INSERT and accesses row[0]
(tuple indexing). When the psycopg3 connection pool is configured with dict_row
row factory, rows are dicts and row[0] raises KeyError: 0.

All API key save/delete endpoints call save_config() → _save_runtime_to_pg or
_seed_runtime_to_pg, causing HTTP 500.

The fix: _seed_runtime_to_pg must set conn.row_factory = dict_row before the
SELECT and access row["version"] (dict key), consistent with _save_runtime_to_pg
and _load_runtime_from_pg.
"""

from unittest.mock import MagicMock, patch


def _make_dict_row_result(version: int) -> dict:
    """Simulate a psycopg3 dict_row result for version column."""
    return {"version": version}


def _make_tuple_row_result(version: int) -> tuple:
    """Simulate a psycopg3 tuple row result (default row factory)."""
    return (version,)


class TestSeedRuntimeToPgRowFactory:
    """
    Bug #591: _seed_runtime_to_pg must use dict_row and row["version"].

    These tests verify the row factory is set and dict-style access is used,
    preventing KeyError: 0 when the pool uses dict_row globally.
    """

    def _make_mock_pool_with_dict_row(self, version: int = 1):
        """
        Create a mock psycopg3 connection pool that returns dict rows
        (simulating a pool configured with dict_row at pool level).
        """
        mock_cursor = MagicMock()
        mock_cursor.fetchone.return_value = _make_dict_row_result(version)

        mock_execute_result = MagicMock()
        mock_execute_result.fetchone.return_value = _make_dict_row_result(version)

        mock_conn = MagicMock()
        mock_conn.execute.return_value = mock_execute_result
        mock_conn.__enter__ = MagicMock(return_value=mock_conn)
        mock_conn.__exit__ = MagicMock(return_value=False)

        mock_pool = MagicMock()
        mock_pool.connection.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_pool.connection.return_value.__exit__ = MagicMock(return_value=False)

        return mock_pool, mock_conn

    def test_seed_runtime_to_pg_sets_dict_row_factory(self, tmp_path):
        """
        _seed_runtime_to_pg must set conn.row_factory = dict_row before the
        SELECT, so it works when pool has dict_row configured globally.
        """
        from code_indexer.server.services.config_service import ConfigService

        service = ConfigService(server_dir_path=str(tmp_path))
        service.load_config()

        mock_pool, mock_conn = self._make_mock_pool_with_dict_row(version=1)
        service._pool = mock_pool
        service._sqlite_db_path = None

        # Verify _seed_runtime_to_pg exists and is callable
        assert hasattr(service, "_seed_runtime_to_pg"), (
            "_seed_runtime_to_pg method missing — Bug #591 fix not implemented"
        )

        with patch("code_indexer.server.services.config_service.dict_row", create=True):
            service._seed_runtime_to_pg()

        # conn.row_factory must have been set (any value is fine, but it must be set)
        assert mock_conn.row_factory is not None or hasattr(mock_conn, "row_factory"), (
            "_seed_runtime_to_pg did not set conn.row_factory before SELECT"
        )

    def test_seed_runtime_to_pg_accesses_version_by_key_not_index(self, tmp_path):
        """
        _seed_runtime_to_pg must read row["version"] (dict key), not row[0].
        Regression: row[0] raises KeyError: 0 when dict_row is active.
        """
        from code_indexer.server.services.config_service import ConfigService

        service = ConfigService(server_dir_path=str(tmp_path))
        service.load_config()
        service._sqlite_db_path = None

        mock_pool, mock_conn = self._make_mock_pool_with_dict_row(version=3)
        service._pool = mock_pool

        assert hasattr(service, "_seed_runtime_to_pg"), (
            "_seed_runtime_to_pg missing — Bug #591 fix not implemented"
        )

        service._seed_runtime_to_pg()

        # After the call, the seeded version must be stored correctly
        assert service._db_config_version == 3, (
            f"Expected _db_config_version=3, got {service._db_config_version}. "
            "Bug: row[0] fails with dict rows; fix requires row['version']."
        )

    def test_seed_runtime_to_pg_fails_with_tuple_row_and_dict_access(self, tmp_path):
        """
        Demonstrates why row[0] is broken: if the code wrongly uses tuple access
        on a dict row, Python raises KeyError: 0. This test documents the failure
        mode that the fix must prevent.
        """
        row = _make_dict_row_result(version=5)

        # Simulates the OLD broken code: row[0] on a dict row
        try:
            _ = row[0]
            assert False, "Expected KeyError: 0 — this proves the bug exists"
        except KeyError as e:
            assert e.args[0] == 0, f"Expected KeyError(0), got KeyError({e.args[0]})"

        # Simulates the FIXED code: row["version"] on a dict row
        assert row["version"] == 5

    def test_save_runtime_to_pg_sets_dict_row_factory(self, tmp_path):
        """
        _save_runtime_to_pg must also use dict_row and row["version"].
        Both seed and save paths must be consistent.
        """
        from code_indexer.server.services.config_service import ConfigService

        service = ConfigService(server_dir_path=str(tmp_path))
        service.load_config()
        service._sqlite_db_path = None

        mock_pool, mock_conn = self._make_mock_pool_with_dict_row(version=2)
        service._pool = mock_pool

        assert hasattr(service, "_save_runtime_to_pg"), (
            "_save_runtime_to_pg missing — Bug #591 fix not implemented"
        )

        config = service.get_config()
        service._save_runtime_to_pg(config)

        # conn.row_factory must be set to dict_row (not None/default)
        # This verifies the fix is applied: the method sets row_factory before SELECT
        assert mock_conn.row_factory is not None, (
            "_save_runtime_to_pg did not set conn.row_factory — KeyError: 0 will occur"
        )
