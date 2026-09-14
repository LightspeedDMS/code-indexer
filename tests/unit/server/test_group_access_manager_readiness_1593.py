"""
Unit tests for the AC9 tool-access enforcement readiness gate (Story #1593).

AC9: enforcement at the five MCP sites stays DORMANT (falls back to the
legacy role-based check) until Story 2's `tool_access_migration_complete`
readiness marker reads true anywhere in the fleet. This story only READS
that marker -- Story 2 owns writing it, and Story 2 is not part of this
tree yet, so the marker table this reader queries does not exist on a
fresh database.

Contract established here (since Story 2 doesn't exist yet to define it):
a single-row marker table `tool_access_migration_state` with a `complete
BOOLEAN NOT NULL` column, keyed by `id = 1`. `is_tool_access_enforcement_
ready()` is the single shared decision point AC3/AC4's five enforcement
sites will call.

Safe-default requirement: table/row absent MUST read as False (legacy
fallback), never raise and never default to True -- a node that starts
before Story 2's seeder has run anywhere in the fleet must never
fail-closed-lock every user out.

Live-read requirement: this is NOT decided once at construction/boot --
a marker flipped mid-session (simulating seeding completing on another
cluster node) must be observed by the SAME long-lived instance on its
very next call, with no restart.

TDD: written FIRST, against unmodified production code where
`is_tool_access_enforcement_ready` does not exist at all -- every test
below is expected to fail with AttributeError until the method lands.
"""

import sqlite3
import tempfile
from pathlib import Path

import pytest

from code_indexer.server.services.group_access_manager import GroupAccessManager


@pytest.fixture
def temp_db_path():
    with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
        db_path = Path(f.name)
    yield db_path
    if db_path.exists():
        db_path.unlink()


def _set_marker(db_path: Path, complete: bool) -> None:
    """Simulate Story 2's seeder having written the readiness marker."""
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS tool_access_migration_state "
            "(id INTEGER PRIMARY KEY, complete BOOLEAN NOT NULL)"
        )
        conn.execute(
            "INSERT INTO tool_access_migration_state (id, complete) VALUES (1, ?) "
            "ON CONFLICT(id) DO UPDATE SET complete = excluded.complete",
            (1 if complete else 0,),
        )
        conn.commit()
    finally:
        conn.close()


class TestReadinessSafeDefault:
    def test_false_when_marker_table_does_not_exist(self, temp_db_path):
        manager = GroupAccessManager(temp_db_path)
        assert manager.is_tool_access_enforcement_ready() is False

    def test_does_not_raise_when_marker_table_absent(self, temp_db_path):
        manager = GroupAccessManager(temp_db_path)
        # Must not raise -- absence is the expected, common, safe case.
        manager.is_tool_access_enforcement_ready()


class TestReadinessReflectsMarkerValue:
    def test_true_when_marker_row_complete(self, temp_db_path):
        manager = GroupAccessManager(temp_db_path)
        _set_marker(temp_db_path, complete=True)
        assert manager.is_tool_access_enforcement_ready() is True

    def test_false_when_marker_row_not_complete(self, temp_db_path):
        manager = GroupAccessManager(temp_db_path)
        _set_marker(temp_db_path, complete=False)
        assert manager.is_tool_access_enforcement_ready() is False


class TestReadinessIsLiveNotCachedAtConstruction:
    def test_flip_from_false_to_true_observed_without_restart(self, temp_db_path):
        """A node started while the marker was false/absent, still serving
        traffic, must observe a later flip to true on its NEXT call -- no
        restart, no re-construction of GroupAccessManager."""
        manager = GroupAccessManager(temp_db_path)
        assert manager.is_tool_access_enforcement_ready() is False

        _set_marker(temp_db_path, complete=True)

        assert manager.is_tool_access_enforcement_ready() is True
