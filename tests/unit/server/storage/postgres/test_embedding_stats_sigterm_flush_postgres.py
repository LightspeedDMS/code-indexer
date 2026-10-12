"""PostgreSQL mirror of
tests/unit/server/services/test_embedding_stats_writer_sigterm_flush.py: a
SIGTERMed child writing through EmbeddingCallStatsPostgresBackend persists
its rows within the grace period (including a batch the periodic writer was
inserting when SIGTERM landed), and a locked table or a stuck insert cannot
hold it past that period (skipped without TEST_POSTGRES_DSN)."""

from __future__ import annotations

import os
import signal
import textwrap
import time
from pathlib import Path
from typing import Iterator

import psycopg
import pytest

from code_indexer.utils.process_group import (
    SIGTERM_GRACE_SECONDS,
    terminate_process_group,
)
from tests.unit.server.services.test_embedding_stats_writer_sigterm_flush import (
    spawn_child,
    wait_dead,
)

pytestmark = pytest.mark.timeout(120)

_PG_HEAD = textwrap.dedent(
    """
    import sys
    from pathlib import Path
    from code_indexer.server.services.embedding_call_stats import (
        EmbeddingCallStatsPostgresBackend,
    )
    from code_indexer.server.storage.postgres.connection_pool import ConnectionPool

    backend = EmbeddingCallStatsPostgresBackend(ConnectionPool(sys.argv[1]))
    """
)


@pytest.fixture
def stats_dsn(migrated_scratch_pg_dsn: str) -> Iterator[str]:
    yield migrated_scratch_pg_dsn
    with psycopg.connect(migrated_scratch_pg_dsn, autocommit=True) as conn:
        conn.execute("DELETE FROM embedding_call_stats")


def _rows(dsn: str) -> int:
    with psycopg.connect(dsn) as conn:
        row = conn.execute("SELECT COUNT(*) FROM embedding_call_stats").fetchone()
    assert row is not None
    return int(row[0])


def test_multi_batch_drain_fits_grace_period_postgres(stats_dsn: str) -> None:
    proc = spawn_child(_PG_HEAD, stats_dsn, 2500, "plain")
    started = time.monotonic()
    terminate_process_group(proc)
    elapsed = time.monotonic() - started

    assert proc.returncode == -signal.SIGTERM, f"exit {proc.returncode}"
    assert _rows(stats_dsn) == 2500
    assert elapsed < SIGTERM_GRACE_SECONDS, f"drain took {elapsed:.3f}s"


def test_sigterm_while_main_thread_holds_queue_lock_persists_rows_postgres(
    stats_dsn: str,
) -> None:
    proc = spawn_child(_PG_HEAD, stats_dsn, 50, "signal_in_put")
    elapsed, stderr = wait_dead(proc, SIGTERM_GRACE_SECONDS + 3)

    assert proc.returncode == -signal.SIGTERM, f"exit {proc.returncode}: {stderr}"
    assert _rows(stats_dsn) == 50, stderr[-3000:]
    assert elapsed < SIGTERM_GRACE_SECONDS, f"took {elapsed:.3f}s"


def _sigterm_in_window_pg(dsn: str, tmp_path: Path, mode: str) -> str:
    """Spawn a 50-record child in `mode`, SIGTERM it once the periodic
    writer is inside its insert; assert it ended by SIGTERM inside the grace
    period. Returns its stderr."""
    marker = tmp_path / "window.marker"
    proc = spawn_child(_PG_HEAD, dsn, 50, mode, str(marker))
    deadline = time.monotonic() + 10
    while not marker.exists():
        assert proc.poll() is None, "the child died before the window opened"
        assert time.monotonic() < deadline, "the window never opened"
        time.sleep(0.01)
    os.killpg(proc.pid, signal.SIGTERM)
    elapsed, stderr = wait_dead(proc, SIGTERM_GRACE_SECONDS + 3)
    assert proc.returncode == -signal.SIGTERM, f"exit {proc.returncode}: {stderr}"
    assert elapsed < SIGTERM_GRACE_SECONDS, f"took {elapsed:.3f}s: {stderr}"
    return stderr


def test_sigterm_during_periodic_insert_persists_in_flight_rows_postgres(
    stats_dsn: str, tmp_path: Path
) -> None:
    stderr = _sigterm_in_window_pg(stats_dsn, tmp_path, "inflight_slow")

    assert _rows(stats_dsn) == 50, stderr[-3000:]


def test_sigterm_during_stuck_periodic_insert_counts_in_flight_rows_postgres(
    stats_dsn: str, tmp_path: Path
) -> None:
    stderr = _sigterm_in_window_pg(stats_dsn, tmp_path, "inflight_stuck")

    assert "50 embedding-call stats rows not persisted" in stderr, stderr[-3000:]
    assert _rows(stats_dsn) == 0


def test_locked_stats_table_still_ends_within_grace_and_warns_postgres(
    stats_dsn: str,
) -> None:
    proc = spawn_child(_PG_HEAD, stats_dsn, 2500, "plain")
    with psycopg.connect(stats_dsn) as blocker:
        blocker.execute("LOCK TABLE embedding_call_stats IN EXCLUSIVE MODE")
        started = time.monotonic()
        terminate_process_group(proc)
        elapsed = time.monotonic() - started
        blocker.rollback()
    assert proc.stderr is not None
    stderr = proc.stderr.read()

    assert proc.returncode == -signal.SIGTERM, f"exit {proc.returncode}: {stderr}"
    assert elapsed < SIGTERM_GRACE_SECONDS, f"took {elapsed:.3f}s"
    assert "2500 embedding-call stats rows not persisted" in stderr, stderr[-3000:]
    assert _rows(stats_dsn) == 0
