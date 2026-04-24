"""
Bug #892: lifecycle_backfill JobTracker registration fails with
"Error binding parameter N - probably unsupported type."

Root cause: save_job() in BackgroundJobsSqliteBackend passes progress_info
raw to the SQLite ? binding. SQLite3 accepts only int, float, str, bytes,
and None. A dict triggers the binding error.

The same raw passthrough exists in the three inline INSERT / UPDATE
statements inside job_tracker.py (_insert_job, _atomic_insert_impl,
_upsert_job).

Fix: serialize progress_info via
    json.dumps(v) if isinstance(v, dict) else v
at every write boundary (sqlite_backends.py save_job and update_job,
job_tracker.py three inline paths).

Test strategy (TDD, Messi Rule #1 anti-mock):
  - Use real SQLite databases via DatabaseSchema.initialize_database().
  - No mocks anywhere.
  - RED phase: dict progress_info raises SQLite InterfaceError before fix.
  - GREEN phase: same dict passes after fix; round-trips correctly.

Write boundaries covered:
  1. sqlite_backends.BackgroundJobsSqliteBackend.save_job
  2. sqlite_backends.BackgroundJobsSqliteBackend.update_job
  3. job_tracker._insert_job (backend path)
  4. job_tracker._insert_job (inline conn path)
  5. job_tracker._upsert_job (inline conn path)
  6. job_tracker._atomic_insert_impl (inline conn path)
"""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, Generator, Optional

import pytest


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

_INLINE_SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS background_jobs (
    job_id TEXT PRIMARY KEY NOT NULL,
    operation_type TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    started_at TEXT,
    completed_at TEXT,
    result TEXT,
    error TEXT,
    progress INTEGER NOT NULL DEFAULT 0,
    username TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    cancelled INTEGER NOT NULL DEFAULT 0,
    repo_alias TEXT,
    resolution_attempts INTEGER NOT NULL DEFAULT 0,
    progress_info TEXT,
    metadata TEXT
)
"""

_ACTIVE_JOB_INDEX_SQL = """
CREATE UNIQUE INDEX IF NOT EXISTS idx_active_job_per_repo
ON background_jobs(operation_type, repo_alias)
WHERE status IN ('pending', 'running')
"""


def _create_inline_db(db_path: Path, *, with_unique_index: bool = False) -> str:
    """
    Create a minimal background_jobs SQLite database at db_path.

    Args:
        db_path: Filesystem path for the new database file.
        with_unique_index: Also create idx_active_job_per_repo when True
            (required by _atomic_insert_impl path).

    Returns:
        Str path to the created database.
    """
    conn = sqlite3.connect(str(db_path))
    try:
        conn.execute(_INLINE_SCHEMA_SQL)
        if with_unique_index:
            conn.execute(_ACTIVE_JOB_INDEX_SQL)
        conn.commit()
    finally:
        conn.close()
    return str(db_path)


def _decode_progress_info(raw: Optional[Any]) -> Optional[Any]:
    """
    Normalise a progress_info value read from the database.

    After the fix, a dict-typed progress_info is JSON-serialized before
    binding, so it comes back as a JSON string. This helper decodes it so
    assertions can compare against the original dict regardless of storage
    format.
    """
    if isinstance(raw, str):
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, ValueError):
            return raw
    return raw


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def backend(tmp_path: Path) -> Generator:
    """Real BackgroundJobsSqliteBackend on a properly initialized schema."""
    from code_indexer.server.storage.database_manager import DatabaseSchema
    from code_indexer.server.storage.sqlite_backends import BackgroundJobsSqliteBackend

    db_path = tmp_path / "test892.db"
    schema = DatabaseSchema(str(db_path))
    schema.initialize_database()
    yield BackgroundJobsSqliteBackend(str(db_path))


@pytest.fixture
def tracker_with_backend(tmp_path: Path):
    """Real JobTracker backed by BackgroundJobsSqliteBackend (backend path)."""
    from code_indexer.server.storage.database_manager import DatabaseSchema
    from code_indexer.server.storage.sqlite_backends import BackgroundJobsSqliteBackend
    from code_indexer.server.services.job_tracker import JobTracker

    db_path = tmp_path / "test892_jt.db"
    schema = DatabaseSchema(str(db_path))
    schema.initialize_database()
    be = BackgroundJobsSqliteBackend(str(db_path))

    jt = JobTracker.__new__(JobTracker)
    jt._active_jobs = {}
    jt._lock = threading.Lock()
    jt._backend = be
    jt._conn_manager = None
    return jt


@pytest.fixture
def tracker_direct(tmp_path: Path):
    """Real JobTracker using inline conn_manager path (no backend)."""
    from code_indexer.server.services.job_tracker import JobTracker

    db_path = tmp_path / "test892_direct.db"
    _create_inline_db(db_path)
    return JobTracker(str(db_path))


# ---------------------------------------------------------------------------
# Tests: BackgroundJobsSqliteBackend.save_job  (write boundary 1)
# ---------------------------------------------------------------------------


class TestSaveJobProgressInfoBug892:
    """
    Bug #892: save_job() with dict progress_info raises SQLite binding error.

    RED anchor for write boundary 1.
    """

    def test_save_job_with_dict_progress_info_round_trips(self, backend) -> None:
        """
        dict progress_info must not raise and must round-trip correctly.
        Before fix: 'Error binding parameter N - probably unsupported type.'
        """
        backend.save_job(
            job_id="bug892-dict-pi",
            operation_type="lifecycle_backfill",
            status="pending",
            created_at="2026-04-23T00:00:00+00:00",
            username="system",
            progress=0,
            # deliberately passing dict to reproduce bug #892 — guarded by type: ignore below
            progress_info={"step": "initializing", "count": 3},  # type: ignore[arg-type]
        )
        job = backend.get_job("bug892-dict-pi")
        assert job is not None
        assert _decode_progress_info(job["progress_info"]) == {
            "step": "initializing",
            "count": 3,
        }

    def test_save_job_with_str_progress_info_still_works(self, backend) -> None:
        """str progress_info must continue to work (non-regression)."""
        backend.save_job(
            job_id="bug892-str-pi",
            operation_type="lifecycle_backfill",
            status="pending",
            created_at="2026-04-23T00:00:00+00:00",
            username="system",
            progress=0,
            progress_info="Initializing lifecycle backfill",
        )
        job = backend.get_job("bug892-str-pi")
        assert job is not None
        assert job["progress_info"] == "Initializing lifecycle backfill"

    def test_save_job_with_none_progress_info_still_works(self, backend) -> None:
        """None progress_info (the lifecycle_backfill nominal case) must work."""
        backend.save_job(
            job_id="bug892-none-pi",
            operation_type="lifecycle_backfill",
            status="pending",
            created_at="2026-04-23T00:00:00+00:00",
            username="system",
            progress=0,
            progress_info=None,
            metadata={"total": 3, "source": "startup_backfill"},
        )
        job = backend.get_job("bug892-none-pi")
        assert job is not None
        assert job["progress_info"] is None
        assert job["metadata"] == {"total": 3, "source": "startup_backfill"}


# ---------------------------------------------------------------------------
# Tests: BackgroundJobsSqliteBackend.update_job  (write boundary 2)
# ---------------------------------------------------------------------------


class TestJobTrackerUpdateJobBug892:
    """
    Bug #892 (update path): update_job() with dict progress_info must not raise
    and must round-trip correctly.

    RED anchor for write boundary 2.
    """

    def test_update_job_with_dict_progress_info_round_trips(self, backend) -> None:
        """dict progress_info must serialize, not raise, and round-trip."""
        backend.save_job(
            job_id="bug892-upd-dict-pi",
            operation_type="lifecycle_backfill",
            status="pending",
            created_at="2026-04-23T00:00:00+00:00",
            username="system",
            progress=0,
        )
        backend.update_job(
            "bug892-upd-dict-pi",
            progress_info={"step": "processing", "done": 1, "total": 3},
        )
        job = backend.get_job("bug892-upd-dict-pi")
        assert job is not None
        assert _decode_progress_info(job["progress_info"]) == {
            "step": "processing",
            "done": 1,
            "total": 3,
        }

    def test_update_job_with_str_progress_info_still_works(self, backend) -> None:
        """str progress_info update must still work (non-regression)."""
        backend.save_job(
            job_id="bug892-upd-str-pi",
            operation_type="lifecycle_backfill",
            status="running",
            created_at="2026-04-23T00:00:00+00:00",
            username="system",
            progress=10,
        )
        backend.update_job("bug892-upd-str-pi", progress_info="Processing repo 1/3")
        job = backend.get_job("bug892-upd-str-pi")
        assert job is not None
        assert job["progress_info"] == "Processing repo 1/3"


# ---------------------------------------------------------------------------
# Tests: _insert_job backend path  (write boundary 3)
# ---------------------------------------------------------------------------


class TestJobTrackerRegisterLifecycleBackfillBug892:
    """
    Bug #892 integration: register_job with lifecycle_backfill must succeed,
    persist a row, emit no warning, and handle dict progress_info correctly.

    RED anchor for write boundary 3 (_insert_job -> backend.save_job).
    """

    def test_register_job_lifecycle_backfill_persists_row(
        self, tracker_with_backend
    ) -> None:
        """Simulates the exact call in _run_lifecycle_backfill_async."""
        job_id = str(uuid.uuid4())
        job = tracker_with_backend.register_job(
            job_id=job_id,
            operation_type="lifecycle_backfill",
            username="system",
            metadata={"total": 3, "source": "startup_backfill"},
        )
        assert job.job_id == job_id
        assert job.status == "pending"
        assert job.metadata == {"total": 3, "source": "startup_backfill"}

        retrieved = tracker_with_backend._backend.get_job(job_id)
        assert retrieved is not None
        assert retrieved["metadata"] == {"total": 3, "source": "startup_backfill"}

    def test_register_job_emits_no_warning_for_lifecycle_backfill(
        self, tracker_with_backend, caplog: pytest.LogCaptureFixture
    ) -> None:
        """register_job must not emit WARNING or ERROR for lifecycle_backfill."""
        import logging

        job_id = str(uuid.uuid4())
        with caplog.at_level(logging.WARNING):
            tracker_with_backend.register_job(
                job_id=job_id,
                operation_type="lifecycle_backfill",
                username="system",
                metadata={"total": 2, "source": "startup_backfill"},
            )
        warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
        assert warnings == [], (
            f"Unexpected warnings: {[r.getMessage() for r in warnings]}"
        )

    def test_insert_job_backend_path_dict_progress_info_round_trips(
        self, tracker_with_backend
    ) -> None:
        """
        _insert_job backend path: dict progress_info must not raise and must
        round-trip to the original dict.

        RED anchor for write boundary 3.
        """
        from code_indexer.server.services.job_tracker import TrackedJob

        expected_pi: Dict[str, Any] = {"current": 0, "total": 5}
        job = TrackedJob(
            job_id=str(uuid.uuid4()),
            operation_type="lifecycle_backfill",
            status="pending",
            username="system",
            # deliberately passing dict to reproduce bug #892 — guarded by type: ignore below
            progress_info=expected_pi,  # type: ignore[arg-type]
            metadata={"total": 5, "source": "startup_backfill"},
        )
        tracker_with_backend._insert_job(job)

        retrieved = tracker_with_backend._backend.get_job(job.job_id)
        assert retrieved is not None
        assert _decode_progress_info(retrieved["progress_info"]) == expected_pi


# ---------------------------------------------------------------------------
# Tests: _insert_job inline path  (write boundary 4)
# ---------------------------------------------------------------------------


class TestJobTrackerInsertJobInlineBug892:
    """
    Bug #892 inline INSERT path: raw conn.execute INSERT in _insert_job must
    guard against dict progress_info.

    RED anchor for write boundary 4.
    """

    def test_register_job_none_progress_info_nominal_case(self, tracker_direct) -> None:
        """Nominal case (progress_info=None) must work on inline path."""
        job_id = str(uuid.uuid4())
        job = tracker_direct.register_job(
            job_id=job_id,
            operation_type="lifecycle_backfill",
            username="system",
            metadata={"total": 3, "source": "startup_backfill"},
        )
        assert job.job_id == job_id
        assert job.status == "pending"

    def test_insert_job_inline_dict_progress_info_round_trips(
        self, tracker_direct
    ) -> None:
        """
        _insert_job inline path: dict progress_info must not raise and
        must round-trip correctly.

        RED anchor for write boundary 4.
        """
        from code_indexer.server.services.job_tracker import TrackedJob

        expected_pi: Dict[str, Any] = {"step": "init", "count": 2}
        job = TrackedJob(
            job_id=str(uuid.uuid4()),
            operation_type="lifecycle_backfill",
            status="pending",
            username="system",
            # deliberately passing dict to reproduce bug #892 — guarded by type: ignore below
            progress_info=expected_pi,  # type: ignore[arg-type]
            metadata={"total": 2, "source": "startup_backfill"},
        )
        tracker_direct._insert_job(job)

        retrieved = tracker_direct.get_job(job.job_id)
        assert retrieved is not None
        assert _decode_progress_info(retrieved.progress_info) == expected_pi

    def test_insert_job_inline_str_progress_info_non_regression(
        self, tracker_direct
    ) -> None:
        """str progress_info must still work on inline INSERT path."""
        from code_indexer.server.services.job_tracker import TrackedJob

        job = TrackedJob(
            job_id=str(uuid.uuid4()),
            operation_type="lifecycle_backfill",
            status="pending",
            username="system",
            progress_info="step 1 of 3",
        )
        tracker_direct._insert_job(job)

        retrieved = tracker_direct.get_job(job.job_id)
        assert retrieved is not None
        assert retrieved.progress_info == "step 1 of 3"


# ---------------------------------------------------------------------------
# Tests: _upsert_job and _atomic_insert_impl inline paths (write boundaries 5, 6)
# ---------------------------------------------------------------------------


class TestJobTrackerUpsertAndAtomicInlineBug892:
    """
    Bug #892 inline UPDATE and atomic INSERT paths.

    Write boundary 5: _upsert_job inline conn UPDATE
    Write boundary 6: _atomic_insert_impl inline conn INSERT
    """

    def test_upsert_job_inline_dict_progress_info_round_trips(
        self, tracker_direct
    ) -> None:
        """
        _upsert_job inline UPDATE path: dict progress_info must not raise
        and must round-trip correctly.

        RED anchor for write boundary 5.
        """
        from code_indexer.server.services.job_tracker import TrackedJob
        from datetime import datetime, timezone

        job_id = str(uuid.uuid4())
        tracker_direct.register_job(
            job_id=job_id,
            operation_type="lifecycle_backfill",
            username="system",
        )

        expected_pi: Dict[str, Any] = {"done": 1, "total": 3}
        job = TrackedJob(
            job_id=job_id,
            operation_type="lifecycle_backfill",
            status="running",
            username="system",
            progress=50,
            # deliberately passing dict to reproduce bug #892 — guarded by type: ignore below
            progress_info=expected_pi,  # type: ignore[arg-type]
            started_at=datetime.now(timezone.utc),
        )
        tracker_direct._upsert_job(job)

        # Read directly from SQLite — _upsert_job bypasses _active_jobs in memory,
        # so get_job would return the stale pending snapshot.
        updated = tracker_direct._load_job_from_sqlite(job_id)
        assert updated is not None
        assert updated.status == "running"
        assert _decode_progress_info(updated.progress_info) == expected_pi

    def test_update_status_inline_str_progress_info_non_regression(
        self, tracker_direct
    ) -> None:
        """str progress_info via update_status must work on inline path."""
        job_id = str(uuid.uuid4())
        tracker_direct.register_job(
            job_id=job_id,
            operation_type="lifecycle_backfill",
            username="system",
        )
        tracker_direct.update_status(
            job_id, status="running", progress=25, progress_info="Processing 1/3"
        )
        job = tracker_direct.get_job(job_id)
        assert job is not None
        assert job.progress_info == "Processing 1/3"
        assert job.progress == 25

    def test_atomic_insert_impl_inline_dict_progress_info_round_trips(
        self, tmp_path: Path
    ) -> None:
        """
        _atomic_insert_impl inline INSERT path: dict progress_info must not
        raise and must round-trip correctly.

        Requires idx_active_job_per_repo (with_unique_index=True).

        RED anchor for write boundary 6.
        """
        from code_indexer.server.services.job_tracker import JobTracker, TrackedJob

        db_path = tmp_path / "test892_atomic.db"
        _create_inline_db(db_path, with_unique_index=True)
        jt = JobTracker(str(db_path))

        expected_pi: Dict[str, Any] = {"step": "atomic_init"}
        job = TrackedJob(
            job_id=str(uuid.uuid4()),
            operation_type="lifecycle_backfill",
            status="pending",
            username="system",
            repo_alias="test-repo",
            # deliberately passing dict to reproduce bug #892 — guarded by type: ignore below
            progress_info=expected_pi,  # type: ignore[arg-type]
            metadata={"total": 1},
        )
        jt._atomic_insert_impl(job)

        retrieved = jt.get_job(job.job_id)
        assert retrieved is not None
        assert _decode_progress_info(retrieved.progress_info) == expected_pi
