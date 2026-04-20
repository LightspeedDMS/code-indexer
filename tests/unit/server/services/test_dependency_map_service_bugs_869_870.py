"""
Unit tests for Bug #869 and Bug #870.

Bug #869 — CRASH: RefreshScheduler missing set_active_backfill_job_id
    Every delta refresh crashes with:
        AttributeError: 'RefreshScheduler' object has no attribute 'set_active_backfill_job_id'

Bug #870 — SQL Binding Failure in lifecycle_backfill Job Registration
    lifecycle_backfill: JobTracker registration failed (non-fatal):
        Error binding parameter 7 - probably unsupported type.
    Root cause: BackgroundJobsSqliteBackend.update_job does not JSON-serialize 'metadata'.
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock, Mock


def _make_refresh_scheduler(tmp_path):
    """Build a minimal RefreshScheduler for testing (no real SQLite registry)."""
    from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
    from code_indexer.global_repos.query_tracker import QueryTracker

    config_source = MagicMock()
    config_source.get_all_repos.return_value = []
    mock_registry = Mock()
    mock_registry.get_all_repos.return_value = []

    return RefreshScheduler(
        golden_repos_dir=str(tmp_path),
        config_source=config_source,
        query_tracker=QueryTracker(),
        cleanup_manager=Mock(),
        registry=mock_registry,
    )


def _make_service_with_real_tracker(tmp_path):
    """Build DependencyMapService with a real JobTracker backed by SQLite."""
    from code_indexer.server.services.dependency_map_service import DependencyMapService
    from code_indexer.server.services.job_tracker import JobTracker
    from code_indexer.server.storage.sqlite_backends import BackgroundJobsSqliteBackend
    from code_indexer.server.storage.database_manager import DatabaseSchema

    db_path = str(tmp_path / "tracker.db")
    DatabaseSchema(db_path).initialize_database()
    backend = BackgroundJobsSqliteBackend(db_path)
    tracker = JobTracker(db_path=db_path, storage_backend=backend)

    config_mgr = Mock()
    server_config = Mock()
    server_config.cluster = None
    config_mgr.load_config.return_value = server_config

    return DependencyMapService(
        golden_repos_manager=Mock(),
        config_manager=config_mgr,
        tracking_backend=Mock(),
        analyzer=Mock(),
        job_tracker=tracker,
    )


# ---------------------------------------------------------------------------
# Bug #869 — set_active_backfill_job_id must exist on RefreshScheduler
# ---------------------------------------------------------------------------


def test_set_active_backfill_job_id_stores_value(tmp_path):
    """Bug #869: set_active_backfill_job_id() must store the job_id on the scheduler."""
    scheduler = _make_refresh_scheduler(tmp_path)
    job_id = "lifecycle-backfill-abc12345"

    # Must not raise AttributeError
    scheduler.set_active_backfill_job_id(job_id)

    assert scheduler._active_backfill_job_id == job_id


def test_set_active_backfill_job_id_accepts_none(tmp_path):
    """Bug #869: set_active_backfill_job_id(None) must store None on the scheduler."""
    scheduler = _make_refresh_scheduler(tmp_path)

    scheduler.set_active_backfill_job_id("lifecycle-backfill-abc12345")
    scheduler.set_active_backfill_job_id(None)

    assert scheduler._active_backfill_job_id is None


# ---------------------------------------------------------------------------
# Bug #870 — metadata must be JSON-serialized in BackgroundJobsSqliteBackend
# ---------------------------------------------------------------------------


def test_backfill_register_aggregate_job_returns_job_id(tmp_path):
    """Bug #870: _backfill_register_aggregate_job must return a non-None job_id
    when a real SQLite-backed JobTracker is present and cluster_wide_total > 0."""
    svc = _make_service_with_real_tracker(tmp_path)

    result = svc._backfill_register_aggregate_job(cluster_wide_total=10)

    assert result is not None, (
        "_backfill_register_aggregate_job returned None — likely the metadata "
        "dict binding error caused the exception handler to absorb the failure."
    )
    assert isinstance(result, str)
    assert result.startswith("lifecycle-backfill-")


def test_backfill_register_aggregate_job_metadata_serialized_in_backend(tmp_path):
    """Bug #870: BackgroundJobsSqliteBackend.update_job with dict metadata must not
    raise 'Error binding parameter 7 - probably unsupported type'."""
    from code_indexer.server.storage.sqlite_backends import BackgroundJobsSqliteBackend
    from code_indexer.server.storage.database_manager import DatabaseSchema

    db_path = str(tmp_path / "test_jobs.db")
    DatabaseSchema(db_path).initialize_database()
    backend = BackgroundJobsSqliteBackend(db_path)

    job_id = "lifecycle-backfill-test0001"
    backend.save_job(
        job_id=job_id,
        operation_type="lifecycle_backfill",
        status="pending",
        created_at=datetime.now(timezone.utc).isoformat(),
        username="system",
        progress=0,
    )

    metadata = {
        "cluster_wide_total": 42,
        "processed": 0,
        "disclaimer": "some text",
        "owner_node_id": None,
        "stage": "processing",
    }

    # Must not raise "Error binding parameter 7 - probably unsupported type"
    backend.update_job(
        job_id,
        status="running",
        metadata=metadata,
    )

    row = backend.get_job(job_id)
    assert row is not None
    stored_meta = row.get("metadata")
    assert isinstance(stored_meta, dict), (
        f"Expected dict but got {type(stored_meta)}: {stored_meta!r}"
    )
    assert stored_meta["cluster_wide_total"] == 42
