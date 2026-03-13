"""
Tests for DatabaseHealthService, specifically:
- Story #18: Add database size to tooltip
- Bug #435: _check_not_locked must use an independent sqlite3.connect() call

Tests for _format_file_size() helper function and get_tooltip() with size.
"""

import os
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch, call as mock_call

import pytest

from code_indexer.server.services.database_health_service import (
    DatabaseHealthResult,
    DatabaseHealthStatus,
    CheckResult,
    DatabaseHealthService,
    _format_file_size,
)


class TestFormatFileSize:
    """Tests for _format_file_size() helper function."""

    def test_format_file_size_zero_bytes(self):
        """Zero bytes should format as '0 B'."""
        assert _format_file_size(0) == "0 B"

    def test_format_file_size_bytes_small(self):
        """Small byte values under 1 KB should format as 'X B'."""
        assert _format_file_size(512) == "512 B"

    def test_format_file_size_bytes_boundary(self):
        """1023 bytes (just under 1 KB) should still be in bytes."""
        assert _format_file_size(1023) == "1023 B"

    def test_format_file_size_kilobytes_exact(self):
        """Exactly 1 KB should format as '1.0 KB'."""
        assert _format_file_size(1024) == "1.0 KB"

    def test_format_file_size_kilobytes_decimal(self):
        """KB values should show one decimal place."""
        # 46285 bytes = 45.2001... KB
        assert _format_file_size(46285) == "45.2 KB"

    def test_format_file_size_kilobytes_boundary(self):
        """Just under 1 MB should still be in KB."""
        # 1048575 bytes = 1023.999... KB
        assert _format_file_size(1048575) == "1024.0 KB"

    def test_format_file_size_megabytes_exact(self):
        """Exactly 1 MB should format as '1.0 MB'."""
        assert _format_file_size(1048576) == "1.0 MB"

    def test_format_file_size_megabytes_decimal(self):
        """MB values should show one decimal place."""
        # 134742016 bytes = 128.5 MB
        assert _format_file_size(134742016) == "128.5 MB"

    def test_format_file_size_megabytes_boundary(self):
        """Just under 1 GB should still be in MB."""
        # 1073741823 bytes = 1023.999... MB
        assert _format_file_size(1073741823) == "1024.0 MB"

    def test_format_file_size_gigabytes_exact(self):
        """Exactly 1 GB should format as '1.00 GB' (2 decimal places for precision)."""
        assert _format_file_size(1073741824) == "1.00 GB"

    def test_format_file_size_gigabytes_decimal(self):
        """GB values should show two decimal places for precision."""
        # 1342177280 bytes = 1.25 GB
        assert _format_file_size(1342177280) == "1.25 GB"

    def test_format_file_size_large_gigabytes(self):
        """Large GB values should format correctly (2 decimal places for precision)."""
        # 10737418240 bytes = 10.00 GB
        assert _format_file_size(10737418240) == "10.00 GB"


class TestGetTooltipWithSize:
    """Tests for get_tooltip() method including file size."""

    def test_tooltip_includes_size_for_existing_file(self):
        """Tooltip should include 'Size:' line for existing database file."""
        # Create a temporary file with known size
        with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as f:
            # Write 5 KB of data
            f.write(b"x" * 5120)
            temp_path = f.name

        try:
            result = DatabaseHealthResult(
                file_name="test.db",
                display_name="Test Database",
                status=DatabaseHealthStatus.HEALTHY,
                checks={"connect": CheckResult(passed=True)},
                db_path=temp_path,
            )

            tooltip = result.get_tooltip()

            assert "Test Database" in tooltip
            assert temp_path in tooltip
            assert "Size: 5.0 KB" in tooltip
        finally:
            os.unlink(temp_path)

    def test_tooltip_omits_size_when_file_not_found(self):
        """Tooltip should omit size line when file doesn't exist."""
        result = DatabaseHealthResult(
            file_name="missing.db",
            display_name="Missing Database",
            status=DatabaseHealthStatus.HEALTHY,
            checks={"connect": CheckResult(passed=True)},
            db_path="/nonexistent/path/missing.db",
        )

        tooltip = result.get_tooltip()

        assert "Missing Database" in tooltip
        assert "/nonexistent/path/missing.db" in tooltip
        assert "Size:" not in tooltip

    def test_tooltip_size_appears_between_path_and_error(self):
        """For unhealthy DB, size should appear between path and error info."""
        # Create a temporary file with known size
        with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as f:
            # Write 10 KB of data
            f.write(b"y" * 10240)
            temp_path = f.name

        try:
            result = DatabaseHealthResult(
                file_name="unhealthy.db",
                display_name="Unhealthy Database",
                status=DatabaseHealthStatus.WARNING,
                checks={
                    "connect": CheckResult(passed=True),
                    "not_locked": CheckResult(
                        passed=False, error_message="Database locked"
                    ),
                },
                db_path=temp_path,
            )

            tooltip = result.get_tooltip()
            lines = tooltip.split("\n")

            # Expected order:
            # Line 0: display_name
            # Line 1: db_path
            # Line 2: Size: X KB
            # Line 3: Not Locked: Database locked
            assert len(lines) == 4
            assert lines[0] == "Unhealthy Database"
            assert lines[1] == temp_path
            assert lines[2] == "Size: 10.0 KB"
            assert "Not Locked" in lines[3]
            assert "Database locked" in lines[3]
        finally:
            os.unlink(temp_path)

    def test_tooltip_healthy_with_existing_file(self):
        """Healthy database with existing file should show name, path, size."""
        with tempfile.NamedTemporaryFile(delete=False, suffix=".db") as f:
            # Write 1 MB of data
            f.write(b"z" * 1048576)
            temp_path = f.name

        try:
            result = DatabaseHealthResult(
                file_name="healthy.db",
                display_name="Healthy Database",
                status=DatabaseHealthStatus.HEALTHY,
                checks={
                    "connect": CheckResult(passed=True),
                    "read": CheckResult(passed=True),
                    "write": CheckResult(passed=True),
                    "integrity": CheckResult(passed=True),
                    "not_locked": CheckResult(passed=True),
                },
                db_path=temp_path,
            )

            tooltip = result.get_tooltip()
            lines = tooltip.split("\n")

            assert len(lines) == 3
            assert lines[0] == "Healthy Database"
            assert lines[1] == temp_path
            assert lines[2] == "Size: 1.0 MB"
        finally:
            os.unlink(temp_path)

    def test_tooltip_error_status_without_file(self):
        """Error status DB without file should show name, path, error - no size."""
        result = DatabaseHealthResult(
            file_name="error.db",
            display_name="Error Database",
            status=DatabaseHealthStatus.ERROR,
            checks={
                "connect": CheckResult(
                    passed=False, error_message="Connection failed: file not found"
                ),
            },
            db_path="/missing/error.db",
        )

        tooltip = result.get_tooltip()
        lines = tooltip.split("\n")

        # Expected: name, path, error (no size since file doesn't exist)
        assert len(lines) == 3
        assert lines[0] == "Error Database"
        assert lines[1] == "/missing/error.db"
        assert "Connect" in lines[2]
        assert "Connection failed" in lines[2]
        assert "Size:" not in tooltip


class TestCheckNotLockedUsesIndependentConnection:
    """
    Bug #435 fix: _check_not_locked() must use a fresh, independent
    sqlite3.connect() call rather than the shared thread-local connection
    from DatabaseConnectionManager.

    Using BEGIN IMMEDIATE on the shared connection can corrupt in-flight
    transactions from other code on the same thread. The fix restores the
    original behaviour of using a dedicated, isolated connection that is
    opened, used, and closed entirely within this method.
    """

    def test_check_not_locked_calls_sqlite3_connect_directly(
        self, tmp_path: Path
    ) -> None:
        """
        _check_not_locked() must call sqlite3.connect() directly, NOT via
        DatabaseConnectionManager, so it gets an independent connection that
        cannot contaminate the shared thread-local connection state.
        """
        db_path = tmp_path / "test_lock.db"
        # Create a real empty SQLite database so the file exists
        conn = sqlite3.connect(str(db_path))
        conn.close()

        connect_calls = []
        real_connect = sqlite3.connect

        def tracking_connect(path, *args, **kwargs):
            connect_calls.append(path)
            return real_connect(path, *args, **kwargs)

        with patch("sqlite3.connect", side_effect=tracking_connect):
            result = DatabaseHealthService._check_not_locked(str(db_path))

        # The method must have called sqlite3.connect() directly
        assert len(connect_calls) >= 1, (
            "_check_not_locked() must call sqlite3.connect() directly "
            "to get an isolated connection for lock testing"
        )
        assert result.passed is True

    def test_check_not_locked_does_not_use_database_connection_manager(
        self, tmp_path: Path
    ) -> None:
        """
        _check_not_locked() must NOT call DatabaseConnectionManager.get_instance()
        because using the shared connection would corrupt in-flight transactions.
        """
        db_path = tmp_path / "test_lock2.db"
        conn = sqlite3.connect(str(db_path))
        conn.close()

        manager_calls = []

        # Patch get_instance to track calls
        real_get_instance = __import__(
            "code_indexer.server.storage.database_manager",
            fromlist=["DatabaseConnectionManager"],
        ).DatabaseConnectionManager.get_instance

        def tracking_get_instance(path):
            manager_calls.append(path)
            return real_get_instance(path)

        with patch(
            "code_indexer.server.services.database_health_service."
            "DatabaseConnectionManager.get_instance",
            side_effect=tracking_get_instance,
        ):
            result = DatabaseHealthService._check_not_locked(str(db_path))

        # The method must NOT have called DatabaseConnectionManager.get_instance()
        assert len(manager_calls) == 0, (
            "_check_not_locked() must not call DatabaseConnectionManager.get_instance(); "
            f"it was called {len(manager_calls)} time(s) with args: {manager_calls}"
        )
        assert result.passed is True

    def test_check_not_locked_closes_its_own_connection(
        self, tmp_path: Path
    ) -> None:
        """
        The independent connection opened by _check_not_locked() must be
        explicitly closed after use to avoid a resource leak.
        """
        db_path = tmp_path / "test_lock3.db"
        conn = sqlite3.connect(str(db_path))
        conn.close()

        real_connect = sqlite3.connect
        created_connections = []

        def capturing_connect(path, *args, **kwargs):
            c = real_connect(path, *args, **kwargs)
            created_connections.append(c)
            return c

        with patch("sqlite3.connect", side_effect=capturing_connect):
            result = DatabaseHealthService._check_not_locked(str(db_path))

        assert result.passed is True
        assert len(created_connections) >= 1

        # Verify the connection is closed (in Python, sqlite3 Connection.close()
        # raises ProgrammingError on subsequent use if already closed)
        for c in created_connections:
            try:
                c.execute("SELECT 1")
                # If this succeeds, the connection was NOT closed - that is a bug
                pytest.fail(
                    "_check_not_locked() did not close its independent connection"
                )
            except Exception:
                # Any exception here means the connection is already closed - correct
                pass


class TestManagerPathChecks:
    """
    Regression tests for Bug #435: _check_connect, _check_read, _check_write,
    and _check_integrity must use DatabaseConnectionManager (not sqlite3.connect
    directly).

    Strategy: assert that DatabaseConnectionManager.get_instance() IS called by
    each check method. This is the positive-assertion equivalent of "must use the
    manager" and avoids the difficulty of distinguishing the health-service's own
    direct sqlite3 calls from the manager's internal ones (both share the same
    sqlite3 module object, so module-namespace patching cannot separate them).
    """

    def _make_real_db(self, tmp_path: Path, name: str) -> str:
        """Create a real SQLite database file and return its path as string."""
        db_path = tmp_path / name
        conn = sqlite3.connect(str(db_path))
        conn.close()
        return str(db_path)

    _GET_INSTANCE_PATCH = (
        "code_indexer.server.services.database_health_service."
        "DatabaseConnectionManager.get_instance"
    )

    def test_check_connect_uses_database_connection_manager(
        self, tmp_path: Path
    ) -> None:
        """_check_connect() must call DatabaseConnectionManager.get_instance()."""
        db_path = self._make_real_db(tmp_path, "connect_test.db")

        real_get_instance = __import__(
            "code_indexer.server.storage.database_manager",
            fromlist=["DatabaseConnectionManager"],
        ).DatabaseConnectionManager.get_instance

        instance_calls = []

        def tracking_get_instance(path):
            instance_calls.append(path)
            return real_get_instance(path)

        with patch(self._GET_INSTANCE_PATCH, side_effect=tracking_get_instance):
            result = DatabaseHealthService._check_connect(db_path)

        assert result.passed is True
        assert len(instance_calls) >= 1, (
            "_check_connect() must call DatabaseConnectionManager.get_instance() "
            "to obtain its connection"
        )

    def test_check_read_uses_database_connection_manager(
        self, tmp_path: Path
    ) -> None:
        """_check_read() must call DatabaseConnectionManager.get_instance()."""
        db_path = self._make_real_db(tmp_path, "read_test.db")

        real_get_instance = __import__(
            "code_indexer.server.storage.database_manager",
            fromlist=["DatabaseConnectionManager"],
        ).DatabaseConnectionManager.get_instance

        instance_calls = []

        def tracking_get_instance(path):
            instance_calls.append(path)
            return real_get_instance(path)

        with patch(self._GET_INSTANCE_PATCH, side_effect=tracking_get_instance):
            result = DatabaseHealthService._check_read(db_path)

        assert result.passed is True
        assert len(instance_calls) >= 1, (
            "_check_read() must call DatabaseConnectionManager.get_instance() "
            "to obtain its connection"
        )

    def test_check_write_uses_database_connection_manager(
        self, tmp_path: Path
    ) -> None:
        """_check_write() must call DatabaseConnectionManager.get_instance()."""
        db_path = self._make_real_db(tmp_path, "write_test.db")

        real_get_instance = __import__(
            "code_indexer.server.storage.database_manager",
            fromlist=["DatabaseConnectionManager"],
        ).DatabaseConnectionManager.get_instance

        instance_calls = []

        def tracking_get_instance(path):
            instance_calls.append(path)
            return real_get_instance(path)

        with patch(self._GET_INSTANCE_PATCH, side_effect=tracking_get_instance):
            result = DatabaseHealthService._check_write(db_path)

        assert result.passed is True
        assert len(instance_calls) >= 1, (
            "_check_write() must call DatabaseConnectionManager.get_instance() "
            "to obtain its connection"
        )

    def test_check_integrity_uses_database_connection_manager(
        self, tmp_path: Path
    ) -> None:
        """_check_integrity() must call DatabaseConnectionManager.get_instance()."""
        db_path = self._make_real_db(tmp_path, "integrity_test.db")

        real_get_instance = __import__(
            "code_indexer.server.storage.database_manager",
            fromlist=["DatabaseConnectionManager"],
        ).DatabaseConnectionManager.get_instance

        instance_calls = []

        def tracking_get_instance(path):
            instance_calls.append(path)
            return real_get_instance(path)

        with patch(self._GET_INSTANCE_PATCH, side_effect=tracking_get_instance):
            result = DatabaseHealthService._check_integrity(db_path)

        assert result.passed is True
        assert len(instance_calls) >= 1, (
            "_check_integrity() must call DatabaseConnectionManager.get_instance() "
            "to obtain its connection"
        )
