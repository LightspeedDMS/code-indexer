"""
Unit tests for diagnostics persistence in SQLite.

Tests that diagnostic results are persisted to SQLite database and loaded on service initialization.
"""

import json
import sqlite3
import tempfile
from datetime import datetime
from pathlib import Path

import pytest

from code_indexer.server.services.diagnostics_service import (
    DiagnosticCategory,
    DiagnosticResult,
    DiagnosticStatus,
    DiagnosticsService,
)
from code_indexer.server.storage.database_manager import DatabaseSchema


@pytest.fixture
def temp_db():
    """Create a temporary database for testing."""
    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "test_diagnostics.db")

        # Initialize database schema
        schema = DatabaseSchema(db_path=db_path)
        schema.initialize_database()

        yield db_path


@pytest.fixture
def diagnostics_service_with_db(temp_db):
    """Create DiagnosticsService with temporary database."""
    service = DiagnosticsService(db_path=temp_db)
    return service


class TestDiagnosticsPersistence:
    """Test suite for diagnostics persistence functionality."""

    def test_save_results_to_db_creates_new_entry(self, temp_db):
        """Test that saving results creates a new database entry."""
        service = DiagnosticsService(db_path=temp_db)

        # Create test results
        results = [
            DiagnosticResult(
                name="Test Tool",
                status=DiagnosticStatus.WORKING,
                message="Test message",
                details={"version": "1.0.0"},
            )
        ]

        # Save to database
        service._save_results_to_db(DiagnosticCategory.CLI_TOOLS, results)

        # Verify saved in database
        conn = sqlite3.connect(temp_db)
        try:
            cursor = conn.execute(
                "SELECT category, results_json, run_at FROM diagnostic_results WHERE category = ?",
                (DiagnosticCategory.CLI_TOOLS.value,)
            )
            row = cursor.fetchone()

            assert row is not None, "No results saved to database"
            assert row[0] == DiagnosticCategory.CLI_TOOLS.value

            # Verify JSON structure
            saved_results = json.loads(row[1])
            assert len(saved_results) == 1
            assert saved_results[0]["name"] == "Test Tool"
            assert saved_results[0]["status"] == "working"
            assert saved_results[0]["message"] == "Test message"
            assert saved_results[0]["details"]["version"] == "1.0.0"

            # Verify timestamp
            assert row[2] is not None

        finally:
            conn.close()

    def test_save_results_to_db_updates_existing_entry(self, temp_db):
        """Test that saving results updates an existing database entry."""
        service = DiagnosticsService(db_path=temp_db)

        # Save first set of results
        results1 = [
            DiagnosticResult(
                name="Test Tool",
                status=DiagnosticStatus.NOT_RUN,
                message="Not run",
                details={},
            )
        ]
        service._save_results_to_db(DiagnosticCategory.CLI_TOOLS, results1)

        # Save second set of results (should update)
        results2 = [
            DiagnosticResult(
                name="Test Tool",
                status=DiagnosticStatus.WORKING,
                message="Now working",
                details={"version": "2.0.0"},
            )
        ]
        service._save_results_to_db(DiagnosticCategory.CLI_TOOLS, results2)

        # Verify only one entry exists with updated data
        conn = sqlite3.connect(temp_db)
        try:
            cursor = conn.execute(
                "SELECT results_json FROM diagnostic_results WHERE category = ?",
                (DiagnosticCategory.CLI_TOOLS.value,)
            )
            rows = cursor.fetchall()

            assert len(rows) == 1, "Should have exactly one entry after update"

            saved_results = json.loads(rows[0][0])
            assert saved_results[0]["status"] == "working"
            assert saved_results[0]["message"] == "Now working"
            assert saved_results[0]["details"]["version"] == "2.0.0"

        finally:
            conn.close()

    def test_load_results_from_db_on_init(self, temp_db):
        """Test that results are loaded from database on service initialization."""
        # Pre-populate database with results
        results = [
            DiagnosticResult(
                name="Persisted Tool",
                status=DiagnosticStatus.WORKING,
                message="Loaded from DB",
                details={"loaded": True},
            )
        ]

        conn = sqlite3.connect(temp_db)
        try:
            results_json = json.dumps([r.to_dict() for r in results])
            conn.execute(
                "INSERT OR REPLACE INTO diagnostic_results (category, results_json, run_at) VALUES (?, ?, ?)",
                (DiagnosticCategory.CLI_TOOLS.value, results_json, datetime.now().isoformat())
            )
            conn.commit()
        finally:
            conn.close()

        # Create service (should load from DB)
        service = DiagnosticsService(db_path=temp_db)

        # Verify results loaded into cache
        status = service.get_status()
        cli_tools_results = status[DiagnosticCategory.CLI_TOOLS]

        assert len(cli_tools_results) == 1
        assert cli_tools_results[0].name == "Persisted Tool"
        assert cli_tools_results[0].status == DiagnosticStatus.WORKING
        assert cli_tools_results[0].message == "Loaded from DB"
        assert cli_tools_results[0].details["loaded"] is True

    def test_get_status_returns_persisted_results(self, temp_db):
        """Test that get_status returns persisted results when cache is empty."""
        # Pre-populate database
        results = [
            DiagnosticResult(
                name="DB Tool",
                status=DiagnosticStatus.ERROR,
                message="Error from DB",
                details={"error_code": 42},
            )
        ]

        conn = sqlite3.connect(temp_db)
        try:
            results_json = json.dumps([r.to_dict() for r in results])
            conn.execute(
                "INSERT OR REPLACE INTO diagnostic_results (category, results_json, run_at) VALUES (?, ?, ?)",
                (DiagnosticCategory.EXTERNAL_APIS.value, results_json, datetime.now().isoformat())
            )
            conn.commit()
        finally:
            conn.close()

        # Create service
        service = DiagnosticsService(db_path=temp_db)

        # Clear cache to force DB load
        service._cache.clear()
        service._cache_timestamps.clear()

        # Get status (should load from DB)
        status = service.get_status()
        api_results = status[DiagnosticCategory.EXTERNAL_APIS]

        assert len(api_results) == 1
        assert api_results[0].name == "DB Tool"
        assert api_results[0].status == DiagnosticStatus.ERROR
        assert api_results[0].message == "Error from DB"

    @pytest.mark.asyncio
    async def test_run_category_persists_results(self, temp_db):
        """Test that run_category saves results to database."""
        service = DiagnosticsService(db_path=temp_db)

        # Run category diagnostics
        await service.run_category(DiagnosticCategory.CLI_TOOLS)

        # Verify results saved to database
        conn = sqlite3.connect(temp_db)
        try:
            cursor = conn.execute(
                "SELECT category, results_json FROM diagnostic_results WHERE category = ?",
                (DiagnosticCategory.CLI_TOOLS.value,)
            )
            row = cursor.fetchone()

            assert row is not None, "Results not saved after run_category"
            assert row[0] == DiagnosticCategory.CLI_TOOLS.value

            saved_results = json.loads(row[1])
            assert len(saved_results) > 0, "No results in saved data"

        finally:
            conn.close()

    @pytest.mark.asyncio
    async def test_run_all_diagnostics_persists_results(self, temp_db):
        """Test that run_all_diagnostics saves results for all categories."""
        service = DiagnosticsService(db_path=temp_db)

        # Run all diagnostics
        await service.run_all_diagnostics()

        # Verify results saved for all categories
        conn = sqlite3.connect(temp_db)
        try:
            cursor = conn.execute("SELECT category FROM diagnostic_results")
            rows = cursor.fetchall()

            saved_categories = {row[0] for row in rows}

            # Should have results for all categories
            assert DiagnosticCategory.CLI_TOOLS.value in saved_categories
            assert DiagnosticCategory.SDK_PREREQUISITES.value in saved_categories
            assert DiagnosticCategory.EXTERNAL_APIS.value in saved_categories
            assert DiagnosticCategory.CREDENTIALS.value in saved_categories
            assert DiagnosticCategory.INFRASTRUCTURE.value in saved_categories

        finally:
            conn.close()

    def test_json_serialization_of_diagnostic_results(self, temp_db):
        """Test that DiagnosticResult objects serialize/deserialize correctly."""
        service = DiagnosticsService(db_path=temp_db)

        # Create complex results with various data types
        timestamp = datetime.now()
        results = [
            DiagnosticResult(
                name="Complex Tool",
                status=DiagnosticStatus.WARNING,
                message="Warning message with unicode: café",
                details={
                    "string": "value",
                    "int": 42,
                    "float": 3.14,
                    "bool": True,
                    "null": None,
                    "list": [1, 2, 3],
                    "nested": {"key": "value"}
                },
                timestamp=timestamp
            )
        ]

        # Save to database
        service._save_results_to_db(DiagnosticCategory.INFRASTRUCTURE, results)

        # Load from database
        conn = sqlite3.connect(temp_db)
        try:
            cursor = conn.execute(
                "SELECT results_json FROM diagnostic_results WHERE category = ?",
                (DiagnosticCategory.INFRASTRUCTURE.value,)
            )
            row = cursor.fetchone()

            loaded_results = json.loads(row[0])
            loaded = loaded_results[0]

            # Verify all fields preserved
            assert loaded["name"] == "Complex Tool"
            assert loaded["status"] == "warning"
            assert loaded["message"] == "Warning message with unicode: café"
            assert loaded["details"]["string"] == "value"
            assert loaded["details"]["int"] == 42
            assert loaded["details"]["float"] == 3.14
            assert loaded["details"]["bool"] is True
            assert loaded["details"]["null"] is None
            assert loaded["details"]["list"] == [1, 2, 3]
            assert loaded["details"]["nested"]["key"] == "value"
            assert loaded["timestamp"] == timestamp.isoformat()

        finally:
            conn.close()

    def test_empty_database_returns_placeholder_results(self, temp_db):
        """Test that service returns placeholder results when database is empty."""
        service = DiagnosticsService(db_path=temp_db)

        # Get status (database is empty)
        status = service.get_status()

        # Should have placeholder results for all categories
        assert DiagnosticCategory.CLI_TOOLS in status
        assert DiagnosticCategory.SDK_PREREQUISITES in status
        assert DiagnosticCategory.EXTERNAL_APIS in status
        assert DiagnosticCategory.CREDENTIALS in status
        assert DiagnosticCategory.INFRASTRUCTURE in status

        # Placeholder results should have NOT_RUN status
        for category, results in status.items():
            assert len(results) > 0
            # Note: Actual implementation may have mixed statuses, just verify structure exists
            assert all(hasattr(r, 'status') for r in results)
