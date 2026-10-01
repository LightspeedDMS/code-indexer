"""
Unit tests proving Bug #1894 RC3's scheduled-submission breaker gate
(RefreshScheduler._submit_refresh_job -> _scheduled_local_repo_repair_is_
quarantined, added this mission in refresh_scheduler.py) is genuinely
backend-agnostic: it must stop the tight 5-min global_repo_refresh retry
and record state correctly when `golden_repo_metadata` is a
GoldenRepoMetadataPostgresBackend, not just the SQLite backend already
covered by test_local_repo_repair_circuit_breaker_1769.py.

Mirrors the mocked-pool convention used by
tests/unit/server/storage/postgres/test_local_repo_repair_quarantine_state_1769.py
and test_refresh_integrity_quarantine_state_1506.py: a MagicMock connection
pool exercises the real GoldenRepoMetadataPostgresBackend SQL/psycopg-v3
code path (matching this project's faithful-DB-mock discipline,
feedback_faithful_db_mocks) -- no live PostgreSQL required. The scheduler
under test is the REAL RefreshScheduler, wired with the PG-backed metadata
store via the same `golden_repo_metadata_backend=` injection point
production code uses; only the PostgreSQL connection pool is a test
double.
"""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, Mock

import pytest

from code_indexer.global_repos.refresh_scheduler import (
    RefreshScheduler,
    _LOCAL_REPO_REPAIR_QUARANTINE_THRESHOLD,
)
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.server.storage.postgres.golden_repo_metadata_backend import (
    GoldenRepoMetadataPostgresBackend,
)

ALIAS = "postgres_breaker_repo-global"
REPO_NAME = "postgres_breaker_repo"

# Arbitrary placeholder for the mocked config source -- the breaker gate
# under test never reads this value, it only needs config_source to be a
# well-formed collaborator (mirrors test_local_repo_repair_circuit_breaker_
# 1769.py's identical mock_config_source fixture).
_UNUSED_MOCK_REFRESH_INTERVAL_SECONDS = 3600


def _make_mock_pg_pool(fetchone_return=None):
    """Same convention as test_local_repo_repair_quarantine_state_1769.py."""
    mock_cursor = MagicMock()
    mock_cursor.fetchone.return_value = fetchone_return

    mock_conn = MagicMock()
    mock_conn.cursor.return_value.__enter__ = MagicMock(return_value=mock_cursor)
    mock_conn.cursor.return_value.__exit__ = MagicMock(return_value=False)

    mock_pool = MagicMock()

    @contextmanager
    def _connection():
        yield mock_conn

    mock_pool.connection.side_effect = _connection

    return mock_pool, mock_conn, mock_cursor


@pytest.fixture
def golden_repos_dir(tmp_path):
    golden_dir = tmp_path / "golden-repos"
    golden_dir.mkdir(parents=True)
    return golden_dir


def _make_scheduler(golden_repos_dir: Path, pg_backend) -> RefreshScheduler:
    mock_config_source = Mock()
    mock_config_source.get_global_refresh_interval.return_value = (
        _UNUSED_MOCK_REFRESH_INTERVAL_SECONDS
    )
    mock_registry = Mock()
    mock_registry.get_global_repo.return_value = {
        "alias_name": ALIAS,
        "repo_url": "local://postgres_breaker_repo",
    }
    mock_registry.list_global_repos.return_value = []
    mock_registry.update_refresh_timestamp.return_value = None
    return RefreshScheduler(
        golden_repos_dir=str(golden_repos_dir),
        config_source=mock_config_source,
        query_tracker=Mock(spec=QueryTracker),
        cleanup_manager=Mock(spec=CleanupManager),
        registry=mock_registry,
        golden_repo_metadata_backend=pg_backend,
    )


class TestPostgresBackedScheduledSubmissionBreaker:
    def test_quarantined_state_blocks_scheduled_submission_on_postgres(
        self, golden_repos_dir
    ):
        """At-threshold PG-persisted state + still-corrupt config.json must
        block the scheduled global_repo_refresh submission entirely --
        exactly one read query, zero writes, zero job submissions."""
        pool, _conn, cursor = _make_mock_pg_pool(
            fetchone_return=(
                ALIAS,
                _LOCAL_REPO_REPAIR_QUARANTINE_THRESHOLD,
                "repeated repair failure",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:05:00+00:00",
            )
        )
        pg_backend = GoldenRepoMetadataPostgresBackend(pool)
        scheduler = _make_scheduler(golden_repos_dir, pg_backend)
        scheduler.background_job_manager = Mock()
        scheduler.background_job_manager.submit_job.return_value = "unexpected"

        # config.json intentionally left ABSENT -- mirrors the still-broken
        # repo Bug #1894 describes; the external repair has not landed.
        assert scheduler._submit_refresh_job(ALIAS) is None
        scheduler.background_job_manager.submit_job.assert_not_called()

        select_calls = [
            c for c in cursor.execute.call_args_list if "SELECT" in str(c[0][0]).upper()
        ]
        write_calls = [
            c
            for c in cursor.execute.call_args_list
            if "DELETE" in str(c[0][0]).upper() or "INSERT" in str(c[0][0]).upper()
        ]
        assert len(select_calls) == 1
        assert not write_calls

    def test_external_repair_resets_postgres_state_and_submission_proceeds(
        self, golden_repos_dir
    ):
        """At-threshold PG-persisted state + a NOW-valid config.json (the
        external repair succeeded) must reset the PG quarantine row (a real
        DELETE against the PG backend) and let the scheduled job through."""
        pool, conn, cursor = _make_mock_pg_pool(
            fetchone_return=(
                ALIAS,
                _LOCAL_REPO_REPAIR_QUARANTINE_THRESHOLD,
                "repeated repair failure",
                "2026-01-01T00:00:00+00:00",
                "2026-01-01T00:05:00+00:00",
            )
        )
        pg_backend = GoldenRepoMetadataPostgresBackend(pool)
        scheduler = _make_scheduler(golden_repos_dir, pg_backend)
        scheduler.background_job_manager = Mock()
        scheduler.background_job_manager.submit_job.return_value = "refresh-job"

        config_path = golden_repos_dir / REPO_NAME / ".code-indexer" / "config.json"
        config_path.parent.mkdir(parents=True)
        config_path.write_text("{}")

        assert scheduler._submit_refresh_job(ALIAS) == "refresh-job"
        scheduler.background_job_manager.submit_job.assert_called_once()

        delete_calls = [
            c for c in cursor.execute.call_args_list if "DELETE" in str(c[0][0]).upper()
        ]
        assert len(delete_calls) == 1
        assert delete_calls[0][0][1] == (ALIAS,)
        assert "?" not in str(delete_calls[0][0][0])
        conn.commit.assert_called()
