"""Bug #2123: the dashboard job list must show every running and pending job
in the shared store, not only the jobs in the rendering node's memory.

The dashboard "Running" card counts running rows in the shared database
(``JobTracker.get_active_job_count``); the list below it comes from
``JobTracker.get_recent_jobs``.  A job started on ANOTHER node is not in this
node's ``_active_jobs``, and once 20 newer jobs exist it is outside the
"most recently created" history window too, so the card counted it while the
list never showed it.

The scenario helpers are shared with the PostgreSQL mirror
(tests/unit/server/storage/postgres/
test_job_tracker_recent_jobs_cluster_active_postgres_2123.py): rows written by
"another node" go through a real backend, and the reader is a fresh
JobTracker whose memory holds none of them.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import pytest

from code_indexer.server.services import job_tracker as job_tracker_module
from code_indexer.server.services.job_tracker import JobTracker

OTHER_NODE = "node-b"
OLD_RUNNING_ID = "other-node-long-refresh"
OLD_PENDING_ID = "other-node-queued-refresh"
HISTORY_COUNT = 25
LIST_LIMIT = 20
ZERO = timedelta(0)


def _iso(delta: timedelta) -> str:
    return (datetime.now(timezone.utc) - delta).isoformat()


def _save(
    writer: Any,
    job_id: str,
    status: str,
    created_ago: timedelta,
    operation_type: str = "global_repo_refresh",
    completed: bool = False,
) -> None:
    writer.save_job(
        job_id=job_id,
        operation_type=operation_type,
        status=status,
        created_at=_iso(created_ago),
        username="admin",
        progress=100 if completed else 10,
        started_at=None if status == "pending" else _iso(created_ago),
        completed_at=_iso(created_ago - timedelta(seconds=1)) if completed else None,
        # One repository per job: idx_active_job_per_repo allows a single
        # active job per (operation_type, repo_alias).
        repo_alias=f"example-repo-{job_id}",
        executing_node=OTHER_NODE,
    )


def seed_other_node_history(writer: Any) -> None:
    """HISTORY_COUNT completed jobs, all created within the last hour."""
    for i in range(HISTORY_COUNT):
        _save(
            writer,
            f"history-{i:03d}",
            "completed",
            timedelta(minutes=50 - i),
            operation_type="data_retention_cleanup",
            completed=True,
        )


def _ids(jobs: List[Dict[str, Any]]) -> List[str]:
    return [j["job_id"] for j in jobs]


def run_other_node_active_jobs_listed(writer: Any, reader: JobTracker) -> None:
    """Another node's old running + pending jobs, followed by 25 newer
    completed jobs, must all be visible to a reader with empty memory."""
    _save(writer, OLD_RUNNING_ID, "running", timedelta(days=3))
    _save(writer, OLD_PENDING_ID, "pending", timedelta(hours=30))
    seed_other_node_history(writer)

    jobs = reader.get_recent_jobs(limit=LIST_LIMIT, time_filter="24h")
    ids = _ids(jobs)

    assert OLD_RUNNING_ID in ids
    assert OLD_PENDING_ID in ids
    assert ids[0] == OLD_RUNNING_ID
    assert ids[1] == OLD_PENDING_ID
    # Active jobs first, then exactly LIST_LIMIT history rows, no duplicates.
    assert len(ids) == 2 + LIST_LIMIT
    assert len(set(ids)) == len(ids)


def run_card_and_list_agree(writer: Any, reader: JobTracker) -> None:
    """The card's DB counts equal the running/pending rows the list shows,
    including a job of the reader's own (in memory AND in the store)."""
    _save(writer, OLD_RUNNING_ID, "running", timedelta(days=3))
    _save(writer, OLD_PENDING_ID, "pending", timedelta(hours=30))
    seed_other_node_history(writer)
    reader.register_job("own-node-running", "dep_map_analysis", "admin")
    reader.update_status("own-node-running", status="running")

    jobs = reader.get_recent_jobs(limit=LIST_LIMIT, time_filter="24h")
    listed_running = [j for j in jobs if j["status"] == "running"]
    listed_pending = [j for j in jobs if j["status"] == "pending"]

    assert sorted(_ids(listed_running)) == sorted([OLD_RUNNING_ID, "own-node-running"])
    assert _ids(listed_pending) == [OLD_PENDING_ID]
    assert len(listed_running) == reader.get_active_job_count()
    assert len(listed_pending) == reader.get_pending_job_count()


def run_store_row_wins_over_memory(
    writer: Any, reader: JobTracker, check_card: bool = True
) -> None:
    """The shared-store row wins over this node's in-memory copy of the same
    job; memory supplies only jobs the store does not hold.  ``check_card``
    is False for a tracker without a backend, whose card counts memory."""
    from code_indexer.server.services.job_tracker import TrackedJob

    # Registered here (memory: pending), then claimed and started by node B.
    reader.register_job(
        "claimed-elsewhere", "global_repo_refresh", "admin", "example-repo-claimed"
    )
    writer.update_job("claimed-elsewhere", status="running", started_at=_iso(ZERO))
    # Running here (memory: running), then cancelled through the store.
    reader.register_job(
        "cancelled-elsewhere", "dep_map_analysis", "admin", "example-repo-cancelled"
    )
    reader.update_status("cancelled-elsewhere", status="running")
    writer.update_job(
        "cancelled-elsewhere", status="cancelled", completed_at=_iso(ZERO)
    )
    # Known only to this node's memory (no store row yet).
    with reader._lock:
        reader._active_jobs["memory-only"] = TrackedJob(
            job_id="memory-only",
            operation_type="dep_map_analysis",
            status="running",
            username="admin",
        )
    seed_other_node_history(writer)

    jobs = reader.get_recent_jobs(limit=LIST_LIMIT, time_filter="24h")
    by_id: Dict[str, List[str]] = {}
    for job in jobs:
        by_id.setdefault(job["job_id"], []).append(job["status"])

    assert by_id["claimed-elsewhere"] == ["running"]
    assert by_id.get("cancelled-elsewhere", ["cancelled"]) == ["cancelled"]
    assert by_id["memory-only"] == ["running"]
    store_running = [
        j for j in jobs if j["status"] == "running" and j["job_id"] != "memory-only"
    ]
    assert len(store_running) == 1
    if check_card:
        assert reader.get_active_job_count() == 1


def run_exclusions_apply_to_active_rows(writer: Any, reader: JobTracker) -> None:
    """Hidden operation types stay hidden for active rows read from the store."""
    _save(writer, OLD_RUNNING_ID, "running", timedelta(days=3))
    _save(writer, "other-node-xray", "running", timedelta(days=2), "xray_search")
    seed_other_node_history(writer)

    jobs = reader.get_recent_jobs(
        limit=LIST_LIMIT,
        time_filter="24h",
        exclude_operation_types=["xray_search"],
    )
    ids = _ids(jobs)

    assert OLD_RUNNING_ID in ids
    assert "other-node-xray" not in ids


def _seed_many_active(writer: Any) -> tuple:
    running = [f"other-node-running-{i}" for i in range(7)]
    pending = [f"other-node-pending-{i}" for i in range(5)]
    for i, job_id in enumerate(running):
        _save(writer, job_id, "running", timedelta(days=3, hours=i))
    for i, job_id in enumerate(pending):
        _save(writer, job_id, "pending", timedelta(days=2, hours=i))
    seed_other_node_history(writer)
    return running, pending


def run_every_active_row_listed_across_pages(
    writer: Any,
    reader: JobTracker,
    monkeypatch: pytest.MonkeyPatch,
    check_card: bool = True,
) -> None:
    """More active rows than one page: every one is listed, exactly once,
    and the list agrees with the card (``check_card``: tracker has a
    backend, so its card reads the store)."""
    monkeypatch.setattr(job_tracker_module, "_ACTIVE_JOBS_PAGE_SIZE", 3)
    running, pending = _seed_many_active(writer)

    jobs = reader.get_recent_jobs(limit=LIST_LIMIT, time_filter="24h")
    listed_running = [j["job_id"] for j in jobs if j["status"] == "running"]
    listed_pending = [j["job_id"] for j in jobs if j["status"] == "pending"]

    assert sorted(listed_running) == sorted(running)
    assert sorted(listed_pending) == sorted(pending)
    if check_card:
        assert len(listed_running) == reader.get_active_job_count()
        assert len(listed_pending) == reader.get_pending_job_count()


# ---------------------------------------------------------------------------
# SQLite: backend path (production solo mode) and the direct-SQLite path
# ---------------------------------------------------------------------------


def _sqlite_db(tmp_path: Path) -> str:
    from code_indexer.server.storage.database_manager import DatabaseSchema

    db_path = str(tmp_path / "cidx_server.db")
    DatabaseSchema(db_path).initialize_database()
    return db_path


def _sqlite_pair(tmp_path: Path, reader_has_backend: bool) -> tuple:
    from code_indexer.server.storage.sqlite_backends import (
        BackgroundJobsSqliteBackend,
    )

    db_path = _sqlite_db(tmp_path)
    writer = BackgroundJobsSqliteBackend(db_path)
    backend: Optional[Any] = (
        BackgroundJobsSqliteBackend(db_path) if reader_has_backend else None
    )
    reader = JobTracker(db_path, storage_backend=backend, node_id="node-a")
    return writer, reader, backend


def run_admin_jobs_running_filter(
    writer: Any, reader_backend: Any, reader: JobTracker
) -> None:
    """/admin/jobs?status_filter=running (the page the Running card links to)
    lists another node's running job exactly once and agrees with the card.
    Real BackgroundJobManager + JobTracker; only the routes module accessors
    are pointed at them."""
    from unittest.mock import patch

    from code_indexer.server.repositories.background_jobs import (
        BackgroundJobManager,
    )
    from code_indexer.server.web import routes

    _save(writer, OLD_RUNNING_ID, "running", timedelta(days=3))
    seed_other_node_history(writer)
    # Constructed before the try: the finally only runs once it exists.
    manager = BackgroundJobManager(storage_backend=reader_backend, node_id="node-a")
    try:
        with (
            patch.object(routes, "_get_background_job_manager", return_value=manager),
            patch.object(routes, "_get_job_tracker", return_value=reader),
        ):
            jobs, total_count, _pages = routes._get_all_jobs(
                status_filter="running", is_admin=True
            )
    finally:
        manager.shutdown()

    assert _ids(jobs) == [OLD_RUNNING_ID]
    assert total_count == reader.get_active_job_count() == 1


@pytest.mark.parametrize("reader_has_backend", [True, False])
def test_other_node_active_jobs_listed_sqlite(
    tmp_path: Path, reader_has_backend: bool
) -> None:
    writer, reader, _backend = _sqlite_pair(tmp_path, reader_has_backend)
    run_other_node_active_jobs_listed(writer, reader)


@pytest.mark.parametrize("reader_has_backend", [True, False])
def test_store_row_wins_over_memory_sqlite(
    tmp_path: Path, reader_has_backend: bool
) -> None:
    writer, reader, _backend = _sqlite_pair(tmp_path, reader_has_backend)
    run_store_row_wins_over_memory(writer, reader, check_card=reader_has_backend)


def test_card_and_list_agree_sqlite_backend(tmp_path: Path) -> None:
    writer, reader, _backend = _sqlite_pair(tmp_path, reader_has_backend=True)
    run_card_and_list_agree(writer, reader)


@pytest.mark.parametrize("reader_has_backend", [True, False])
def test_exclusions_apply_to_active_rows_sqlite(
    tmp_path: Path, reader_has_backend: bool
) -> None:
    writer, reader, _backend = _sqlite_pair(tmp_path, reader_has_backend)
    run_exclusions_apply_to_active_rows(writer, reader)


@pytest.mark.parametrize("reader_has_backend", [True, False])
def test_every_active_row_listed_across_pages_sqlite(
    tmp_path: Path,
    reader_has_backend: bool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    writer, reader, _backend = _sqlite_pair(tmp_path, reader_has_backend)
    run_every_active_row_listed_across_pages(
        writer, reader, monkeypatch, check_card=reader_has_backend
    )


def test_admin_jobs_running_filter_sqlite_backend(tmp_path: Path) -> None:
    writer, reader, backend = _sqlite_pair(tmp_path, reader_has_backend=True)
    run_admin_jobs_running_filter(writer, backend, reader)


def run_page_ceiling_is_logged(
    writer: Any,
    reader: JobTracker,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """The paging loop is bounded by count // page size + margin pages;
    reaching that ceiling on full pages is logged.  Margin 0 with 6 rows at
    page size 3 makes both counted pages full."""
    monkeypatch.setattr(job_tracker_module, "_ACTIVE_JOBS_PAGE_SIZE", 3)
    monkeypatch.setattr(job_tracker_module, "_ACTIVE_JOBS_PAGE_MARGIN", 0)
    running = [f"other-node-running-{i}" for i in range(6)]
    for i, job_id in enumerate(running):
        _save(writer, job_id, "running", timedelta(days=3, hours=i))

    with caplog.at_level(logging.WARNING, logger=job_tracker_module.__name__):
        jobs = reader.get_recent_jobs(limit=LIST_LIMIT, time_filter="24h")

    listed = [j["job_id"] for j in jobs if j["status"] == "running"]
    assert sorted(listed) == sorted(running)
    assert any("paging ceiling" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("reader_has_backend", [True, False])
def test_page_ceiling_is_logged_sqlite(
    tmp_path: Path,
    reader_has_backend: bool,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    writer, reader, _backend = _sqlite_pair(tmp_path, reader_has_backend)
    run_page_ceiling_is_logged(writer, reader, monkeypatch, caplog)


def run_status_move_between_reads_is_not_missed(
    writer: Any, reader: JobTracker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A job claimed (pending -> running) between two status reads is still
    listed, with its most advanced status."""
    _save(writer, "claimed-mid-read", "pending", timedelta(hours=2))
    # 25 newer rows: the job is outside the history window, so only the
    # active read can list it.
    seed_other_node_history(writer)
    real_fetch = reader._fetch_active_page
    calls: List[str] = []

    def fetch_then_claim(
        status: str, page_size: int, offset: int, exclude: Optional[List[str]]
    ) -> List[Dict[str, Any]]:
        page = real_fetch(status, page_size, offset, exclude)
        calls.append(status)
        if len(calls) == 1:
            writer.update_job(
                "claimed-mid-read", status="running", started_at=_iso(ZERO)
            )
        return page

    monkeypatch.setattr(reader, "_fetch_active_page", fetch_then_claim)
    jobs = reader.get_recent_jobs(limit=LIST_LIMIT, time_filter="24h")

    assert [j["status"] for j in jobs if j["job_id"] == "claimed-mid-read"] == [
        "running"
    ]


@pytest.mark.parametrize("reader_has_backend", [True, False])
def test_status_move_between_reads_is_not_missed_sqlite(
    tmp_path: Path, reader_has_backend: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer, reader, _backend = _sqlite_pair(tmp_path, reader_has_backend)
    run_status_move_between_reads_is_not_missed(writer, reader, monkeypatch)


def run_finished_leftover_evicted_from_memory(
    writer: Any, reader: JobTracker, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An in-memory copy whose store row another node finished is evicted on
    the first render (memory converges), and renders make no per-id store
    lookups -- in-memory ids are resolved by one batched query."""
    from code_indexer.server.services.job_tracker import TrackedJob

    reader.register_job(
        "finished-elsewhere", "global_repo_refresh", "admin", "example-repo-fin"
    )
    writer.update_job("finished-elsewhere", status="completed", completed_at=_iso(ZERO))
    reader.register_job(
        "still-active", "global_repo_refresh", "admin", "example-repo-active"
    )
    with reader._lock:
        reader._active_jobs["memory-only"] = TrackedJob(
            job_id="memory-only",
            operation_type="dep_map_analysis",
            status="running",
            username="admin",
        )
    seed_other_node_history(writer)

    real_lookup = reader._load_job_from_sqlite
    lookups: List[str] = []

    def recording_lookup(job_id: str) -> Any:
        lookups.append(job_id)
        return real_lookup(job_id)

    monkeypatch.setattr(reader, "_load_job_from_sqlite", recording_lookup)

    first = reader.get_recent_jobs(limit=LIST_LIMIT, time_filter="24h")
    with reader._lock:
        in_memory = set(reader._active_jobs)
    assert "finished-elsewhere" not in in_memory
    assert {"still-active", "memory-only"} <= in_memory
    assert all(
        j["status"] != "pending" for j in first if j["job_id"] == "finished-elsewhere"
    )

    reader.get_recent_jobs(limit=LIST_LIMIT, time_filter="24h")
    assert lookups == []


@pytest.mark.parametrize("reader_has_backend", [True, False])
def test_finished_leftover_evicted_from_memory_sqlite(
    tmp_path: Path, reader_has_backend: bool, monkeypatch: pytest.MonkeyPatch
) -> None:
    writer, reader, _backend = _sqlite_pair(tmp_path, reader_has_backend)
    run_finished_leftover_evicted_from_memory(writer, reader, monkeypatch)


@pytest.mark.parametrize("reader_has_backend", [True, False])
def test_render_counts_only_active_statuses_sqlite(
    tmp_path: Path, reader_has_backend: bool
) -> None:
    """The page-ceiling count of a render reads only the active statuses,
    never a GROUP BY over every status of the table (SQL traced)."""
    writer, reader, backend = _sqlite_pair(tmp_path, reader_has_backend)
    _save(writer, OLD_RUNNING_ID, "running", timedelta(days=3))
    seed_other_node_history(writer)
    manager = backend._conn_manager if backend is not None else reader._conn_manager
    conn = manager.get_connection()
    statements: List[str] = []
    conn.set_trace_callback(statements.append)
    try:
        reader.get_recent_jobs(limit=LIST_LIMIT, time_filter="24h")
    finally:
        conn.set_trace_callback(None)

    counts = [s for s in statements if "COUNT(*)" in s]
    assert counts
    assert all("status IN" in s for s in counts), counts


def run_count_filtered_to_statuses(writer: Any) -> None:
    """count_jobs_by_status(statuses=...) counts only the given statuses."""
    _save(writer, OLD_RUNNING_ID, "running", timedelta(days=3))
    _save(writer, OLD_PENDING_ID, "pending", timedelta(hours=30))
    seed_other_node_history(writer)

    counts = writer.count_jobs_by_status(statuses=["running", "pending"])

    assert counts == {"running": 1, "pending": 1}


def test_count_filtered_to_statuses_sqlite_backend(tmp_path: Path) -> None:
    writer, _reader, _backend = _sqlite_pair(tmp_path, reader_has_backend=True)
    run_count_filtered_to_statuses(writer)
