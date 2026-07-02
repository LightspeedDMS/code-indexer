"""
SQLite backend for dependency latency samples.

Story #680: External Dependency Latency Observability

Provides insert, delete, and windowed-select operations for raw latency samples
collected from external dependency calls (HTTP, database, etc.).
"""

import logging
from typing import List
from typing_extensions import TypedDict

from .database_manager import DatabaseConnectionManager

logger = logging.getLogger(__name__)

_REQUIRED_SAMPLE_KEYS = frozenset(
    {"node_id", "dependency_name", "timestamp", "latency_ms", "status_code"}
)


class LatencySample(TypedDict):
    """A single latency measurement for one external dependency call."""

    node_id: str
    dependency_name: str
    timestamp: float
    latency_ms: float
    status_code: int


def _is_real_number(value: object) -> bool:
    """Return True only for int or float, explicitly excluding bool."""
    return type(value) in (int, float)


def _validate_sample(sample: object) -> None:
    """Raise ValueError if sample is not a dict with required keys and correct types."""
    if not isinstance(sample, dict):
        raise ValueError(f"Each sample must be a dict, got {type(sample).__name__}")
    missing = _REQUIRED_SAMPLE_KEYS - set(sample.keys())
    if missing:
        raise ValueError(f"Sample missing required keys: {missing}")
    if not isinstance(sample["node_id"], str) or not sample["node_id"]:
        raise ValueError("node_id must be a non-empty string")
    if not isinstance(sample["dependency_name"], str) or not sample["dependency_name"]:
        raise ValueError("dependency_name must be a non-empty string")
    if not _is_real_number(sample["timestamp"]):
        raise ValueError("timestamp must be a non-bool numeric value")
    if not _is_real_number(sample["latency_ms"]) or sample["latency_ms"] < 0:
        raise ValueError("latency_ms must be a non-negative, non-bool numeric value")
    if type(sample["status_code"]) is not int:
        raise ValueError("status_code must be an int (not bool)")


def _validate_timestamp_arg(value: object, name: str) -> None:
    """Raise ValueError if value is not a real (non-bool) number."""
    if not _is_real_number(value):
        raise ValueError(f"{name} must be a non-bool numeric value")


class DependencyLatencyBackend:
    """
    SQLite storage backend for dependency latency samples.

    Stores raw per-request latency samples and supports windowed queries
    used by the aggregator to compute p50/p95/p99 percentiles.
    """

    def __init__(self, db_path: str) -> None:
        """
        Initialize the backend.

        Args:
            db_path: Path to the SQLite database file (non-empty string).

        Raises:
            ValueError: If db_path is not a non-empty string.
        """
        if not isinstance(db_path, str) or not db_path:
            raise ValueError("db_path must be a non-empty string")
        self._conn_manager = DatabaseConnectionManager.get_instance(db_path)

    def insert_batch(self, samples: List[LatencySample]) -> None:
        """
        Insert a batch of latency samples atomically.

        Args:
            samples: List of LatencySample dicts.

        Raises:
            ValueError: If samples is not a list, or if any sample is malformed.
        """
        if not isinstance(samples, list):
            raise ValueError("samples must be a list")
        if not samples:
            return
        for sample in samples:
            _validate_sample(sample)

        def operation(conn):
            conn.executemany(
                """INSERT INTO dependency_latency_samples
                   (node_id, dependency_name, timestamp, latency_ms, status_code)
                   VALUES (:node_id, :dependency_name, :timestamp,
                           :latency_ms, :status_code)""",
                samples,
            )
            return None

        self._conn_manager.execute_atomic(operation)

    def delete_older_than(self, cutoff_timestamp: float) -> None:
        """
        Delete samples with timestamp strictly less than cutoff_timestamp.

        Args:
            cutoff_timestamp: Unix timestamp (float). Samples older than this
                              are removed to bound storage growth.

        Raises:
            ValueError: If cutoff_timestamp is not a real (non-bool) numeric value.
        """
        _validate_timestamp_arg(cutoff_timestamp, "cutoff_timestamp")

        def operation(conn):
            conn.execute(
                "DELETE FROM dependency_latency_samples WHERE timestamp < ?",
                (cutoff_timestamp,),
            )
            return None

        self._conn_manager.execute_atomic(operation)

    def select_samples_for_window(
        self, start_time: float, end_time: float
    ) -> List[LatencySample]:
        """
        Select all samples within the inclusive time window [start_time, end_time].

        Args:
            start_time: Window start as Unix timestamp (inclusive).
            end_time: Window end as Unix timestamp (inclusive).

        Returns:
            List of LatencySample dicts ordered by timestamp ascending.

        Raises:
            ValueError: If timestamps are not real numeric values or start > end.
        """
        _validate_timestamp_arg(start_time, "start_time")
        _validate_timestamp_arg(end_time, "end_time")
        if start_time > end_time:
            raise ValueError("start_time must be <= end_time")

        def operation(conn):
            cursor = conn.execute(
                """SELECT node_id, dependency_name, timestamp, latency_ms, status_code
                   FROM dependency_latency_samples
                   WHERE timestamp >= ? AND timestamp <= ?
                   ORDER BY timestamp ASC""",
                (start_time, end_time),
            )
            rows = cursor.fetchall()
            return [
                LatencySample(
                    node_id=row[0],
                    dependency_name=row[1],
                    timestamp=row[2],
                    latency_ms=row[3],
                    status_code=row[4],
                )
                for row in rows
            ]

        return self._conn_manager.execute_atomic(operation)
