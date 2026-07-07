"""Cluster-mode bug: postgres save_job rejected current_phase/phase_detail.

The background_jobs table has current_phase + phase_detail columns and the
caller (background_jobs.py) passes both, but BackgroundJobsPostgresBackend.save_job
accepted neither -> every background job in cluster mode raised
`save_job() got an unexpected keyword argument 'current_phase'`. The sqlite
backend accepts + persists both; postgres must too.
"""

from unittest.mock import MagicMock

from code_indexer.server.storage.postgres.background_jobs_backend import (
    BackgroundJobsPostgresBackend,
)


def _backend():
    backend = BackgroundJobsPostgresBackend.__new__(BackgroundJobsPostgresBackend)
    cur = MagicMock()
    conn = MagicMock()
    conn.cursor.return_value.__enter__.return_value = cur
    pool = MagicMock()
    pool.connection.return_value.__enter__.return_value = conn
    backend._pool = pool
    return backend, cur


def test_save_job_accepts_and_persists_phase_fields():
    backend, cur = _backend()
    # Must NOT raise TypeError (the cluster-mode crash), and must persist both.
    backend.save_job(
        job_id="j1",
        operation_type="reindex",
        status="running",
        created_at="2026-07-07T00:00:00Z",
        username="admin",
        progress=0,
        current_phase="semantic",
        phase_detail="indexing files",
    )
    sql, values = cur.execute.call_args[0]
    assert "current_phase" in sql and "phase_detail" in sql
    assert "semantic" in values and "indexing files" in values


def test_save_job_defaults_phase_fields_to_none():
    backend, cur = _backend()
    backend.save_job(
        job_id="j2",
        operation_type="reindex",
        status="running",
        created_at="2026-07-07T00:00:00Z",
        username="admin",
        progress=0,
    )
    _, values = cur.execute.call_args[0]
    # last two positional values are current_phase, phase_detail (both None)
    assert values[-2] is None and values[-1] is None
