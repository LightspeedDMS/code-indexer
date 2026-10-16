"""Bug #2123, PostgreSQL mirror: the dashboard job list (JobTracker
.get_recent_jobs) lists every running/pending job of the shared database,
including jobs another cluster node runs, against a real migrated PostgreSQL
database (skipped without TEST_POSTGRES_DSN).

Scenarios live in
tests/unit/server/services/test_job_tracker_recent_jobs_cluster_active_2123.py.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterator, Tuple

import pytest

from code_indexer.server.services.job_tracker import JobTracker
from tests.unit.server.services.test_job_tracker_recent_jobs_cluster_active_2123 import (
    run_admin_jobs_running_filter,
    run_card_and_list_agree,
    run_count_filtered_to_statuses,
    run_every_active_row_listed_across_pages,
    run_exclusions_apply_to_active_rows,
    run_finished_leftover_evicted_from_memory,
    run_other_node_active_jobs_listed,
    run_page_ceiling_is_logged,
    run_status_move_between_reads_is_not_missed,
    run_store_row_wins_over_memory,
)


@pytest.fixture
def pg_nodes(
    migrated_scratch_pg_dsn: str, tmp_path: Path
) -> Iterator[Tuple[Any, Any, JobTracker]]:
    """The writing node's backend, and the reading node's backend and
    JobTracker, each backend with its own pool on one database."""
    from code_indexer.server.storage.postgres.background_jobs_backend import (
        BackgroundJobsPostgresBackend,
    )
    from code_indexer.server.storage.postgres.connection_pool import ConnectionPool

    writer = BackgroundJobsPostgresBackend(ConnectionPool(migrated_scratch_pg_dsn))
    reader_backend = BackgroundJobsPostgresBackend(
        ConnectionPool(migrated_scratch_pg_dsn)
    )
    reader = JobTracker(
        str(tmp_path / "unused.db"), storage_backend=reader_backend, node_id="node-a"
    )
    try:
        yield writer, reader_backend, reader
    finally:
        with writer._pool.connection() as conn:
            conn.execute("DELETE FROM background_jobs")
        writer.close()
        reader_backend.close()


def test_other_node_active_jobs_listed_postgres(pg_nodes: tuple) -> None:
    writer, _backend, reader = pg_nodes
    run_other_node_active_jobs_listed(writer, reader)


def test_store_row_wins_over_memory_postgres(pg_nodes: tuple) -> None:
    writer, _backend, reader = pg_nodes
    run_store_row_wins_over_memory(writer, reader)


def test_card_and_list_agree_postgres(pg_nodes: tuple) -> None:
    writer, _backend, reader = pg_nodes
    run_card_and_list_agree(writer, reader)


def test_exclusions_apply_to_active_rows_postgres(pg_nodes: tuple) -> None:
    writer, _backend, reader = pg_nodes
    run_exclusions_apply_to_active_rows(writer, reader)


def test_every_active_row_listed_across_pages_postgres(
    pg_nodes: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer, _backend, reader = pg_nodes
    run_every_active_row_listed_across_pages(writer, reader, monkeypatch)


def test_page_ceiling_is_logged_postgres(
    pg_nodes: tuple,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    writer, _backend, reader = pg_nodes
    run_page_ceiling_is_logged(writer, reader, monkeypatch, caplog)


def test_status_move_between_reads_is_not_missed_postgres(
    pg_nodes: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer, _backend, reader = pg_nodes
    run_status_move_between_reads_is_not_missed(writer, reader, monkeypatch)


def test_finished_leftover_evicted_from_memory_postgres(
    pg_nodes: tuple, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer, _backend, reader = pg_nodes
    run_finished_leftover_evicted_from_memory(writer, reader, monkeypatch)


def test_count_filtered_to_statuses_postgres(pg_nodes: tuple) -> None:
    writer, _backend, _reader = pg_nodes
    run_count_filtered_to_statuses(writer)


def test_admin_jobs_running_filter_postgres(pg_nodes: tuple) -> None:
    writer, backend, reader = pg_nodes
    run_admin_jobs_running_filter(writer, backend, reader)
