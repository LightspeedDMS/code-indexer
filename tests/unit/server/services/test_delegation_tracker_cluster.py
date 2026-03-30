"""
Unit tests for DelegationJobTracker DB persistence (Bug #577).

Bug #577: DelegationJobTracker must use DB, not RAM-only.
In cluster mode, callback may arrive on a different node than the one
that started the job. DB is the cross-node source of truth.

Tests use real SQLite — no mocks (MESSI Rule #1).
"""

import sqlite3
import tempfile
from pathlib import Path

import pytest

from src.code_indexer.server.services.delegation_job_tracker import (
    DelegationJobTracker,
    JobResult,
)


def _create_test_db() -> str:
    """Create a temporary SQLite DB with the delegation_job_results table."""
    tmp = tempfile.mktemp(suffix=".db")
    conn = sqlite3.connect(tmp)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS delegation_job_results (
            job_id TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'pending',
            output TEXT,
            exit_code INTEGER,
            error TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            completed_at TEXT
        )
        """
    )
    conn.commit()
    conn.close()
    return tmp


def _make_tracker(db_path: str) -> DelegationJobTracker:
    """Create a fresh DelegationJobTracker wired to SQLite."""
    tracker = DelegationJobTracker()
    tracker.set_sqlite_path(db_path)
    return tracker


class TestDelegationTrackerDbRegister:
    """Test that register_job writes a pending row to DB."""

    @pytest.mark.asyncio
    async def test_register_writes_to_db(self) -> None:
        """
        Given a tracker wired to SQLite,
        When register_job is called,
        Then the DB has a row with status='pending' for that job_id.
        """
        db_path = _create_test_db()
        tracker = _make_tracker(db_path)

        await tracker.register_job("job-001")

        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT job_id, status FROM delegation_job_results WHERE job_id = ?",
            ("job-001",),
        ).fetchone()
        conn.close()

        assert row is not None, "Expected a row in delegation_job_results"
        assert row[0] == "job-001"
        assert row[1] == "pending"

        Path(db_path).unlink(missing_ok=True)


class TestDelegationTrackerDbComplete:
    """Test that complete_job writes a completed row to DB."""

    @pytest.mark.asyncio
    async def test_complete_writes_to_db(self) -> None:
        """
        Given a registered job,
        When complete_job is called,
        Then the DB row has status='completed' and the output/exit_code/error.
        """
        db_path = _create_test_db()
        tracker = _make_tracker(db_path)

        await tracker.register_job("job-002")

        result = JobResult(
            job_id="job-002",
            status="completed",
            output="hello world",
            exit_code=0,
            error=None,
        )
        completed = await tracker.complete_job(result)
        assert completed is True

        conn = sqlite3.connect(db_path)
        row = conn.execute(
            "SELECT status, output, exit_code, error, completed_at "
            "FROM delegation_job_results WHERE job_id = ?",
            ("job-002",),
        ).fetchone()
        conn.close()

        assert row is not None
        assert row[0] == "completed"
        assert row[1] == "hello world"
        assert row[2] == 0
        assert row[3] is None
        assert row[4] is not None  # completed_at should be set

        Path(db_path).unlink(missing_ok=True)


class TestDelegationTrackerDbGetResult:
    """Test that get_result reads from DB."""

    @pytest.mark.asyncio
    async def test_get_result_reads_from_db(self) -> None:
        """
        Given a completed job in the DB (not in local Futures),
        When get_result is called on a fresh tracker sharing the same DB,
        Then it returns the JobResult from the DB.
        """
        db_path = _create_test_db()

        # Simulate node1 completing the job
        tracker1 = _make_tracker(db_path)
        await tracker1.register_job("job-003")
        result = JobResult(
            job_id="job-003",
            status="completed",
            output="from node1",
            exit_code=0,
            error=None,
        )
        await tracker1.complete_job(result)

        # Simulate node2 reading the result (fresh tracker, same DB)
        tracker2 = _make_tracker(db_path)
        fetched = await tracker2.get_result("job-003")

        assert fetched is not None
        assert fetched.job_id == "job-003"
        assert fetched.status == "completed"
        assert fetched.output == "from node1"
        assert fetched.exit_code == 0
        assert fetched.error is None

        Path(db_path).unlink(missing_ok=True)


class TestDelegationTrackerCrossNode:
    """Test cross-node complete and read scenario."""

    @pytest.mark.asyncio
    async def test_cross_node_complete_and_read(self) -> None:
        """
        Given node1 registers a job and node2 completes it via callback,
        When node1 checks for the result,
        Then it finds the completed result from the DB.
        """
        db_path = _create_test_db()

        # Node1 registers
        tracker_node1 = _make_tracker(db_path)
        await tracker_node1.register_job("job-cross-001")

        # Node2 receives callback and completes (fresh tracker, same DB)
        tracker_node2 = _make_tracker(db_path)
        # Node2 must also register the job locally so complete_job finds the Future
        await tracker_node2.register_job("job-cross-001")
        cb_result = JobResult(
            job_id="job-cross-001",
            status="completed",
            output="done by node2",
            exit_code=0,
            error=None,
        )
        await tracker_node2.complete_job(cb_result)

        # Node1 reads result — should find it via DB
        fetched = await tracker_node1.get_result("job-cross-001")
        assert fetched is not None
        assert fetched.job_id == "job-cross-001"
        assert fetched.output == "done by node2"

        Path(db_path).unlink(missing_ok=True)


class TestDelegationTrackerDbHasJob:
    """Test that has_job checks DB."""

    @pytest.mark.asyncio
    async def test_has_job_checks_db(self) -> None:
        """
        Given a job registered in the DB but not in local Futures,
        When has_job is called on a fresh tracker sharing the same DB,
        Then it returns True.
        """
        db_path = _create_test_db()

        # Register on tracker1
        tracker1 = _make_tracker(db_path)
        await tracker1.register_job("job-has-001")

        # Fresh tracker2 sharing same DB — no local Future
        tracker2 = _make_tracker(db_path)
        assert await tracker2.has_job("job-has-001") is True

        Path(db_path).unlink(missing_ok=True)


class TestDelegationTrackerSetConnectionPool:
    """Test that set_connection_pool stores the pool."""

    def test_set_connection_pool(self) -> None:
        """
        Given a tracker,
        When set_connection_pool is called with a pool object,
        Then the pool is stored on the tracker._pool attribute.
        """
        tracker = DelegationJobTracker()
        fake_pool = object()
        tracker.set_connection_pool(fake_pool)
        assert tracker._pool is fake_pool
