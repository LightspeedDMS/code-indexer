"""
Tests for MaintenanceSqliteBackend and BackendRegistry maintenance field.

Story #529: Maintenance Mode Cluster Coordination.
"""

import pytest


class TestMaintenanceBackend:
    @pytest.fixture
    def backend(self, tmp_path):
        from code_indexer.server.storage.sqlite_backends import MaintenanceSqliteBackend

        b = MaintenanceSqliteBackend(str(tmp_path / "t.db"))
        yield b
        b.close()

    def test_satisfies_protocol(self, backend):
        from code_indexer.server.storage.protocols import MaintenanceBackend

        assert isinstance(backend, MaintenanceBackend)

    def test_enter_and_get_status(self, backend):
        backend.enter_maintenance("admin", "Upgrading", "2024-01-01T00:00:00Z")
        status = backend.get_status()
        assert status is not None
        assert status["enabled"] is True
        assert status["reason"] == "Upgrading"

    def test_exit_maintenance(self, backend):
        backend.enter_maintenance("admin", "Test", "2024-01-01T00:00:00Z")
        backend.exit_maintenance()
        status = backend.get_status()
        assert status is None or status.get("enabled") is False

    def test_registry_has_field(self):
        from code_indexer.server.storage.factory import BackendRegistry
        import dataclasses

        assert "maintenance" in {f.name for f in dataclasses.fields(BackendRegistry)}

    def test_get_status_returns_disabled_when_no_row(self, backend):
        """get_status returns a dict with enabled=False when table is empty."""
        status = backend.get_status()
        assert status is not None
        assert status.get("enabled") is False
