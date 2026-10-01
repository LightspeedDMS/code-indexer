"""Tests for the xray_k_calibration BackendRegistry field (Story #1787 S2
amendment AC16, dual-review defect H4/H6 remediation).

SqliteKCalibrationBackend / XrayGraphKCalibrationPostgresBackend both
already existed with full unit/live-PG coverage, but neither was ever
wired into BackendRegistry -- StorageFactory.create_backends() never
constructed either one, so a running server (SQLite or PostgreSQL) had
no way to obtain a shared K-calibration store at all. This is the exact
Bug #1665 registered-but-unwired shape: a backend that is fully correct
in isolation with zero callers.

This wires a dedicated `xray_k_calibration.db` SQLite file (mirroring
`query_embedding_cache.db`'s own dedicated-file precedent, per
k_calibration_store.py's own docstring: "a dedicated DB file... so
calibration writes never contend with main server state") in SQLite
mode, and the shared general ConnectionPool in PostgreSQL mode
(mirroring embedding_call_stats's own established pattern).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


class TestBackendRegistryHasXrayKCalibrationField:
    def test_field_exists_and_defaults_to_none(self) -> None:
        import dataclasses

        from code_indexer.server.storage.factory import BackendRegistry

        fields_by_name = {f.name: f for f in dataclasses.fields(BackendRegistry)}
        assert "xray_k_calibration" in fields_by_name
        assert fields_by_name["xray_k_calibration"].default is None


class TestSqliteModeConstructsRealBackend:
    def test_sqlite_mode_wires_a_real_functional_backend(self, tmp_path: Path) -> None:
        from code_indexer.server.storage.factory import StorageFactory
        from code_indexer.server.services.xray_graph_governor.k_calibration_store import (
            KCalibrationSample,
            SqliteKCalibrationBackend,
        )

        registry = StorageFactory.create_backends(config={}, data_dir=str(tmp_path))

        assert isinstance(registry.xray_k_calibration, SqliteKCalibrationBackend)

        # Anti-Mock: prove it's a REAL, functional backend -- round-trip a
        # sample through the actual SQLite table, not just an isinstance
        # check.
        registry.xray_k_calibration.record_sample(
            KCalibrationSample(
                language="java",
                source_bytes=1000,
                decls=1,
                call_sites=1,
                candidate_edges=1,
                actual_peak_rss=19000,
            )
        )
        assert registry.xray_k_calibration.get_k("java") == 19.0

    def test_sqlite_mode_uses_a_dedicated_db_file_not_the_shared_one(
        self, tmp_path: Path
    ) -> None:
        """Must use its OWN dedicated db file (matching
        SqliteKCalibrationBackend's own docstring contract) -- never the
        shared cidx_server.db other backends in this registry target,
        so calibration writes never contend with main server state."""
        from code_indexer.server.storage.factory import StorageFactory

        registry = StorageFactory.create_backends(config={}, data_dir=str(tmp_path))

        assert registry.xray_k_calibration._db_path == str(
            tmp_path / "xray_k_calibration.db"
        )


class TestPostgresModeConstructsRealBackendType:
    def test_postgres_mode_wires_backend_bound_to_the_general_pool(self) -> None:
        """Mocks ONLY the network boundary (ConnectionPool) -- the wiring
        logic under test (which pool gets passed to which backend
        constructor) runs for real."""
        fake_pool = MagicMock()

        with patch(
            "code_indexer.server.storage.postgres.connection_pool.ConnectionPool",
            return_value=fake_pool,
        ):
            from code_indexer.server.storage.factory import StorageFactory
            from code_indexer.server.storage.postgres.xray_graph_k_calibration_backend import (
                XrayGraphKCalibrationPostgresBackend,
            )

            registry = StorageFactory._create_postgres_backends(
                {"postgres_dsn": "postgresql://x"}
            )

        assert isinstance(
            registry.xray_k_calibration, XrayGraphKCalibrationPostgresBackend
        )
        # Must be bound to the SAME general pool other backends share, not
        # a second isolated pool.
        assert registry.xray_k_calibration._pool is registry.connection_pool


if __name__ == "__main__":
    import sys

    sys.exit(pytest.main([__file__, "-v"]))
