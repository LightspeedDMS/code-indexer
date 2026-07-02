"""
Unit tests for DependencyLatencyBackend - SQLite storage for dependency latency samples.

Story #680: External Dependency Latency Observability

Tests written FIRST following TDD methodology.
"""

import time
from pathlib import Path
from typing import Generator, Optional

import pytest

# Named constants for test values
DEFAULT_NODE_ID = "node-1"
DEFAULT_DEP_NAME = "voyageai_embed"
DEFAULT_LATENCY_MS = 120.5
DEFAULT_STATUS_CODE = 200
LATENCY_FAST_MS = 100.0
LATENCY_SLOW_MS = 200.0
LATENCY_CUSTOM_MS = 55.5
CUSTOM_NODE_ID = "node-42"
POSTGRES_DEP_NAME = "postgres"
GITHUB_DEP_NAME = "github"
STATUS_CODE_TIMEOUT = 0
FLOAT_TOLERANCE = 0.001
WINDOW_PADDING_SECONDS = 1.0
WINDOW_UPPER_PADDING_SECONDS = 2.0
OLD_SAMPLE_AGE_SECONDS = 1000.0
TEST_DB_FILENAME = "test.db"
WINDOW_START_EPOCH = 0.0


@pytest.fixture
def backend(tmp_path: Path) -> Generator:
    """Create a DependencyLatencyBackend with initialized database."""
    from code_indexer.server.storage.database_manager import DatabaseSchema
    from code_indexer.server.storage.dependency_latency_backend import (
        DependencyLatencyBackend,
    )

    db_path = tmp_path / TEST_DB_FILENAME
    schema = DatabaseSchema(str(db_path))
    schema.initialize_database()
    yield DependencyLatencyBackend(str(db_path))


def _make_sample(
    node_id: str = DEFAULT_NODE_ID,
    dependency_name: str = DEFAULT_DEP_NAME,
    timestamp: Optional[float] = None,
    latency_ms: float = DEFAULT_LATENCY_MS,
    status_code: int = DEFAULT_STATUS_CODE,
) -> dict:
    """Helper to create a sample dict with sensible defaults."""
    return {
        "node_id": node_id,
        "dependency_name": dependency_name,
        "timestamp": timestamp if timestamp is not None else time.time(),
        "latency_ms": latency_ms,
        "status_code": status_code,
    }


@pytest.mark.slow
class TestDependencyLatencyBackendInsertBatch:
    """Tests for insert_batch method."""

    def test_insert_batch_inserts_rows(self, backend) -> None:
        """insert_batch() inserts all rows into dependency_latency_samples table."""
        now = time.time()
        samples = [
            _make_sample(timestamp=now, latency_ms=LATENCY_FAST_MS),
            _make_sample(
                timestamp=now + WINDOW_PADDING_SECONDS, latency_ms=LATENCY_SLOW_MS
            ),
        ]
        backend.insert_batch(samples)

        rows = backend.select_samples_for_window(
            now - WINDOW_PADDING_SECONDS, now + WINDOW_UPPER_PADDING_SECONDS
        )
        assert len(rows) == 2

    def test_insert_batch_empty_list_is_noop(self, backend) -> None:
        """insert_batch() with empty list does not raise and inserts nothing."""
        backend.insert_batch([])
        rows = backend.select_samples_for_window(
            WINDOW_START_EPOCH, time.time() + WINDOW_PADDING_SECONDS
        )
        assert rows == []

    def test_insert_batch_stores_all_fields(self, backend) -> None:
        """insert_batch() persists all fields with correct values."""
        now = time.time()
        sample = _make_sample(
            node_id=CUSTOM_NODE_ID,
            dependency_name=POSTGRES_DEP_NAME,
            timestamp=now,
            latency_ms=LATENCY_CUSTOM_MS,
            status_code=STATUS_CODE_TIMEOUT,
        )
        backend.insert_batch([sample])

        rows = backend.select_samples_for_window(
            now - WINDOW_PADDING_SECONDS, now + WINDOW_PADDING_SECONDS
        )
        assert len(rows) == 1
        row = rows[0]
        assert row["node_id"] == CUSTOM_NODE_ID
        assert row["dependency_name"] == POSTGRES_DEP_NAME
        assert abs(row["timestamp"] - now) < FLOAT_TOLERANCE
        assert abs(row["latency_ms"] - LATENCY_CUSTOM_MS) < FLOAT_TOLERANCE
        assert row["status_code"] == STATUS_CODE_TIMEOUT


@pytest.mark.slow
class TestDependencyLatencyBackendDeleteOlderThan:
    """Tests for delete_older_than method."""

    def test_delete_removes_old_samples(self, backend) -> None:
        """delete_older_than() removes samples with timestamp < cutoff."""
        old_time = time.time() - OLD_SAMPLE_AGE_SECONDS
        new_time = time.time()
        backend.insert_batch(
            [
                _make_sample(timestamp=old_time, latency_ms=LATENCY_FAST_MS),
                _make_sample(timestamp=new_time, latency_ms=LATENCY_SLOW_MS),
            ]
        )

        cutoff = old_time + WINDOW_PADDING_SECONDS
        backend.delete_older_than(cutoff)

        rows = backend.select_samples_for_window(
            old_time - WINDOW_PADDING_SECONDS, new_time + WINDOW_PADDING_SECONDS
        )
        assert len(rows) == 1
        assert abs(rows[0]["timestamp"] - new_time) < FLOAT_TOLERANCE

    def test_delete_keeps_samples_at_or_after_cutoff(self, backend) -> None:
        """delete_older_than() keeps samples with timestamp >= cutoff."""
        cutoff = time.time()
        backend.insert_batch(
            [
                _make_sample(timestamp=cutoff, latency_ms=LATENCY_FAST_MS),
                _make_sample(
                    timestamp=cutoff + WINDOW_PADDING_SECONDS,
                    latency_ms=LATENCY_SLOW_MS,
                ),
            ]
        )

        backend.delete_older_than(cutoff)

        rows = backend.select_samples_for_window(
            cutoff - WINDOW_PADDING_SECONDS,
            cutoff + WINDOW_UPPER_PADDING_SECONDS,
        )
        assert len(rows) == 2
