"""
Unit tests for Bug #870.

Bug #870 — SQL Binding Failure in lifecycle_backfill Job Registration
    lifecycle_backfill: JobTracker registration failed (non-fatal):
        Error binding parameter 7 - probably unsupported type.
    Root cause: BackgroundJobsSqliteBackend.update_job does not JSON-serialize 'metadata'.
"""

from datetime import datetime, timezone


# ---------------------------------------------------------------------------
# Bug #870 — metadata must be JSON-serialized in BackgroundJobsSqliteBackend
# ---------------------------------------------------------------------------


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
