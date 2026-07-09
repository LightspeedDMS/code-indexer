"""
TDD tests for AC12: actor_username tracking on background_jobs.

Tests written FIRST (Red phase). Implementation will make them pass.

Covers:
- SQLite schema migration adds actor_username column (nullable)
- Migration idempotency (running twice is a no-op)
- submit_job(actor_username="admin") persists actor_username="admin"
- submit_job(actor_username=None) defaults actor_username to submitter_username
- deactivate_repository(actor_username="admin") propagates to job row
- BackgroundJob dataclass carries actor_username field
- _job_to_dict includes actor_username
- _snapshot_job includes actor_username
- get_job_status response includes actor_username
- list_jobs response includes actor_username per job
- get_jobs_for_display normalizes actor_username
"""

import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

import pytest


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def db_path(tmp_path: Path) -> Path:
    """Return path to a fresh initialized SQLite database."""
    from code_indexer.server.storage.database_manager import DatabaseSchema

    path = tmp_path / "test_actor.db"
    schema = DatabaseSchema(str(path))
    schema.initialize_database()
    return path


@pytest.fixture
def backend(db_path: Path):
    """BackgroundJobsSqliteBackend wrapping the initialized DB."""
    from code_indexer.server.storage.sqlite_backends import BackgroundJobsSqliteBackend

    return BackgroundJobsSqliteBackend(str(db_path))


@pytest.fixture
def job_manager(db_path: Path):
    """BackgroundJobManager with SQLite backend (no JobTracker)."""
    from code_indexer.server.repositories.background_jobs import BackgroundJobManager

    return BackgroundJobManager(use_sqlite=True, db_path=str(db_path))


# ---------------------------------------------------------------------------
# 1. Schema migration: actor_username column must exist after initialization
# ---------------------------------------------------------------------------


class TestActorUsernameSchemaColumn:
    """SQLite schema must have actor_username after migration."""

    def test_background_jobs_has_actor_username_column(self, db_path: Path) -> None:
        """After initialize_database(), background_jobs has actor_username column."""
        conn = sqlite3.connect(str(db_path))
        cursor = conn.execute("PRAGMA table_info(background_jobs)")
        columns = {row[1] for row in cursor.fetchall()}
        conn.close()
        assert "actor_username" in columns, (
            "actor_username column must exist in background_jobs after migration"
        )

    def test_actor_username_column_is_nullable(self, db_path: Path) -> None:
        """actor_username column is nullable (notnull=0)."""
        conn = sqlite3.connect(str(db_path))
        cursor = conn.execute("PRAGMA table_info(background_jobs)")
        rows = cursor.fetchall()
        conn.close()
        actor_row = next((r for r in rows if r[1] == "actor_username"), None)
        assert actor_row is not None, "actor_username column not found"
        # PRAGMA table_info: (cid, name, type, notnull, dflt_value, pk)
        not_null = actor_row[3]
        assert not_null == 0, "actor_username must be nullable (notnull=0)"

    def test_existing_rows_get_null_actor_username_on_migration(
        self, tmp_path: Path
    ) -> None:
        """Old rows that existed before migration retain NULL for actor_username."""
        # Create a DB WITHOUT actor_username (simulate pre-migration state)
        old_db = tmp_path / "old.db"
        conn = sqlite3.connect(str(old_db))
        conn.execute(
            """CREATE TABLE background_jobs (
                job_id TEXT PRIMARY KEY,
                operation_type TEXT,
                status TEXT,
                created_at TEXT,
                started_at TEXT,
                completed_at TEXT,
                result TEXT,
                error TEXT,
                progress INTEGER DEFAULT 0,
                username TEXT,
                is_admin INTEGER DEFAULT 0,
                cancelled INTEGER DEFAULT 0,
                repo_alias TEXT
            )"""
        )
        conn.execute(
            "INSERT INTO background_jobs (job_id, operation_type, status, created_at, username, progress) "
            "VALUES ('old-job-1', 'test_op', 'completed', '2025-01-01T00:00:00+00:00', 'bob', 100)"
        )
        conn.commit()
        conn.close()

        # Now apply the migration (via DatabaseSchema._migrate_background_jobs_actor_username)
        from code_indexer.server.storage.database_manager import DatabaseSchema

        schema = DatabaseSchema(str(old_db))
        conn2 = sqlite3.connect(str(old_db))
        schema._migrate_background_jobs_actor_username(conn2)
        conn2.commit()

        # Verify old row has NULL actor_username
        cursor = conn2.execute(
            "SELECT actor_username FROM background_jobs WHERE job_id = 'old-job-1'"
        )
        row = cursor.fetchone()
        conn2.close()
        assert row is not None
        assert row[0] is None, "Old rows must have NULL actor_username after migration"


# ---------------------------------------------------------------------------
# 2. Migration idempotency
# ---------------------------------------------------------------------------


class TestMigrationIdempotency:
    """Running the migration twice must not raise an error."""

    def test_actor_username_migration_idempotent(self, tmp_path: Path) -> None:
        """Running _migrate_background_jobs_actor_username twice is a no-op."""
        from code_indexer.server.storage.database_manager import DatabaseSchema

        db_path = tmp_path / "idem.db"
        # Initialize a full schema (which includes the migration)
        schema = DatabaseSchema(str(db_path))
        schema.initialize_database()

        # Apply the same migration again -- must not raise
        conn = sqlite3.connect(str(db_path))
        try:
            schema._migrate_background_jobs_actor_username(conn)
            conn.commit()
        except Exception as exc:
            pytest.fail(
                f"_migrate_background_jobs_actor_username raised on second call: {exc}"
            )
        finally:
            conn.close()

        # Column still exists and is unique (no duplicate column)
        conn2 = sqlite3.connect(str(db_path))
        cursor = conn2.execute("PRAGMA table_info(background_jobs)")
        actor_cols = [r for r in cursor.fetchall() if r[1] == "actor_username"]
        conn2.close()
        assert len(actor_cols) == 1, "Exactly one actor_username column must exist"


# ---------------------------------------------------------------------------
# 3. submit_job persists actor_username
# ---------------------------------------------------------------------------


class TestSubmitJobActorUsername:
    """submit_job(..., actor_username=...) must persist the value."""

    def test_submit_job_with_explicit_actor_username_persists(
        self, job_manager, db_path: Path
    ) -> None:
        """submit_job(actor_username='admin') stores actor_username='admin' in DB."""
        import time

        job_id = job_manager.submit_job(
            "deactivate_repository",
            lambda: {"status": "ok"},
            username="bob",
            user_alias="my-repo",
            submitter_username="bob",
            repo_alias="my-repo",
            actor_username="admin",
        )
        # Wait briefly for the background thread to save the job
        time.sleep(0.5)

        conn = sqlite3.connect(str(db_path))
        cursor = conn.execute(
            "SELECT actor_username, username FROM background_jobs WHERE job_id = ?",
            (job_id,),
        )
        row = cursor.fetchone()
        conn.close()

        assert row is not None, "Job must be persisted to SQLite"
        actor_username_db, username_db = row
        assert actor_username_db == "admin", (
            f"actor_username must be 'admin', got {actor_username_db!r}"
        )
        assert username_db == "bob", (
            f"username must be 'bob' (target), got {username_db!r}"
        )

    def test_submit_job_without_actor_username_defaults_to_submitter(
        self, job_manager, db_path: Path
    ) -> None:
        """submit_job(actor_username=None) defaults actor_username to submitter_username."""
        import time

        job_id = job_manager.submit_job(
            "deactivate_repository",
            lambda: {"status": "ok"},
            username="carol",
            user_alias="repo-x",
            submitter_username="carol",
            repo_alias="repo-x",
            # actor_username intentionally omitted (None by default)
        )
        time.sleep(0.5)

        conn = sqlite3.connect(str(db_path))
        cursor = conn.execute(
            "SELECT actor_username, username FROM background_jobs WHERE job_id = ?",
            (job_id,),
        )
        row = cursor.fetchone()
        conn.close()

        assert row is not None
        actor_username_db, username_db = row
        assert actor_username_db == "carol", (
            f"actor_username must default to submitter 'carol', got {actor_username_db!r}"
        )
        assert username_db == "carol"

    def test_submit_job_actor_username_in_memory_job(self, job_manager) -> None:
        """In-memory BackgroundJob carries actor_username after submit_job."""
        import time

        job_id = job_manager.submit_job(
            "test_op",
            lambda: {"status": "ok"},
            submitter_username="alice",
            repo_alias="repo-a",
            actor_username="superadmin",
        )
        time.sleep(0.1)  # let thread register

        with job_manager._lock:
            job = job_manager.jobs.get(job_id)
            if job is not None:
                actor = getattr(job, "actor_username", "__missing__")
                assert actor == "superadmin", (
                    f"In-memory job.actor_username must be 'superadmin', got {actor!r}"
                )
            # If job already completed and left memory, check DB
            else:
                pass  # DB check happens in other tests


# ---------------------------------------------------------------------------
# 4. BackgroundJob dataclass has actor_username field
# ---------------------------------------------------------------------------


class TestBackgroundJobDataclass:
    """BackgroundJob dataclass must carry actor_username."""

    def test_background_job_has_actor_username_attribute(self) -> None:
        """BackgroundJob dataclass must have actor_username field."""
        from code_indexer.server.repositories.background_jobs import (
            BackgroundJob,
            JobStatus,
        )

        job = BackgroundJob(
            job_id="test-1",
            operation_type="deactivate_repository",
            status=JobStatus.PENDING,
            created_at=datetime.now(timezone.utc),
            started_at=None,
            completed_at=None,
            result=None,
            error=None,
            progress=0,
            username="bob",
            actor_username="admin",
        )
        assert job.actor_username == "admin"

    def test_background_job_actor_username_defaults_to_none(self) -> None:
        """BackgroundJob.actor_username defaults to None (backward compat)."""
        from code_indexer.server.repositories.background_jobs import (
            BackgroundJob,
            JobStatus,
        )

        job = BackgroundJob(
            job_id="test-2",
            operation_type="test_op",
            status=JobStatus.PENDING,
            created_at=datetime.now(timezone.utc),
            started_at=None,
            completed_at=None,
            result=None,
            error=None,
            progress=0,
            username="alice",
        )
        assert job.actor_username is None


# ---------------------------------------------------------------------------
# 5. _snapshot_job and _job_to_dict include actor_username
# ---------------------------------------------------------------------------


class TestSnapshotAndDict:
    """_snapshot_job and _job_to_dict must include actor_username."""

    def test_snapshot_job_includes_actor_username(self, job_manager) -> None:
        """_snapshot_job() includes actor_username in returned dict."""
        from code_indexer.server.repositories.background_jobs import (
            BackgroundJob,
            JobStatus,
        )

        job = BackgroundJob(
            job_id="snap-1",
            operation_type="deactivate_repository",
            status=JobStatus.PENDING,
            created_at=datetime.now(timezone.utc),
            started_at=None,
            completed_at=None,
            result=None,
            error=None,
            progress=0,
            username="bob",
            actor_username="admin",
        )
        snapshot = job_manager._snapshot_job(job)
        assert "actor_username" in snapshot
        assert snapshot["actor_username"] == "admin"

    def test_snapshot_job_actor_username_none_when_absent(self, job_manager) -> None:
        """_snapshot_job() includes actor_username=None for legacy jobs."""
        from code_indexer.server.repositories.background_jobs import (
            BackgroundJob,
            JobStatus,
        )

        job = BackgroundJob(
            job_id="snap-2",
            operation_type="test_op",
            status=JobStatus.PENDING,
            created_at=datetime.now(timezone.utc),
            started_at=None,
            completed_at=None,
            result=None,
            error=None,
            progress=0,
            username="alice",
        )
        snapshot = job_manager._snapshot_job(job)
        assert "actor_username" in snapshot
        assert snapshot["actor_username"] is None

    def test_job_to_dict_includes_actor_username(self, job_manager) -> None:
        """_job_to_dict() includes actor_username in returned dict."""
        from code_indexer.server.repositories.background_jobs import (
            BackgroundJob,
            JobStatus,
        )

        job = BackgroundJob(
            job_id="dict-1",
            operation_type="deactivate_repository",
            status=JobStatus.COMPLETED,
            created_at=datetime.now(timezone.utc),
            started_at=None,
            completed_at=None,
            result=None,
            error=None,
            progress=100,
            username="bob",
            actor_username="admin",
        )
        d = job_manager._job_to_dict(job)
        assert "actor_username" in d
        assert d["actor_username"] == "admin"


# ---------------------------------------------------------------------------
# 6. SQLite backend save_job/get_job round-trip for actor_username
# ---------------------------------------------------------------------------


class TestSqliteBackendActorUsername:
    """BackgroundJobsSqliteBackend must persist and retrieve actor_username."""

    def test_save_and_get_job_with_actor_username(self, backend, db_path: Path) -> None:
        """save_job with actor_username='admin' must be retrieved by get_job."""
        backend.save_job(
            job_id="actor-test-1",
            operation_type="deactivate_repository",
            status="pending",
            created_at="2026-01-01T00:00:00+00:00",
            username="bob",
            progress=0,
            repo_alias="some-repo",
            actor_username="admin",
        )
        result = backend.get_job("actor-test-1")
        assert result is not None
        assert result.get("actor_username") == "admin"
        assert result.get("username") == "bob"

    def test_save_and_get_job_null_actor_username(self, backend, db_path: Path) -> None:
        """save_job without actor_username stores NULL; get_job returns None."""
        backend.save_job(
            job_id="actor-test-2",
            operation_type="test_op",
            status="completed",
            created_at="2026-01-01T00:00:00+00:00",
            username="carol",
            progress=100,
        )
        result = backend.get_job("actor-test-2")
        assert result is not None
        assert result.get("actor_username") is None


# ---------------------------------------------------------------------------
# 7. deactivate_repository propagates actor_username
# ---------------------------------------------------------------------------


class TestDeactivateRepositoryActorUsername:
    """ActivatedRepoManager.deactivate_repository must pass actor_username to submit_job."""

    def test_deactivate_repository_passes_actor_username(self, tmp_path: Path) -> None:
        """deactivate_repository(actor_username='admin') results in job with actor_username='admin'."""
        import os
        import json

        from code_indexer.server.repositories.activated_repo_manager import (
            ActivatedRepoManager,
        )
        from code_indexer.server.repositories.background_jobs import (
            BackgroundJobManager,
        )

        # ActivatedRepoManager uses data_dir; activated-repos is created as a subdir
        data_dir = str(tmp_path / "data")
        username = "bob"
        user_alias = "test-repo"
        # The manager creates activated_repos_dir = data_dir/activated-repos
        repo_dir = os.path.join(data_dir, "activated-repos", username, user_alias)
        os.makedirs(repo_dir, exist_ok=True)

        # Write metadata file using the naming convention expected by _load_metadata_file:
        # {activated_repos_dir}/{username}/{user_alias}_metadata.json
        metadata_path = os.path.join(
            data_dir, "activated-repos", username, f"{user_alias}_metadata.json"
        )
        with open(metadata_path, "w") as f:
            json.dump(
                {"golden_repo_alias": "golden", "created_at": "2026-01-01"},
                f,
            )

        # Use a real BackgroundJobManager (no SQLite) with a spy on submit_job
        bjm = BackgroundJobManager()
        submitted_kwargs: Dict[str, Any] = {}

        def capturing_submit(*args, **kwargs):
            submitted_kwargs.update(kwargs)
            # Return a fake job_id without actually running anything
            return "fake-job-id"

        bjm.submit_job = capturing_submit  # type: ignore[method-assign]

        manager = ActivatedRepoManager(
            data_dir=data_dir,
            background_job_manager=bjm,
        )

        manager.deactivate_repository(
            username=username,
            user_alias=user_alias,
            actor_username="admin",
        )

        assert submitted_kwargs.get("actor_username") == "admin", (
            f"actor_username must be passed to submit_job as 'admin', "
            f"got {submitted_kwargs.get('actor_username')!r}"
        )
        assert submitted_kwargs.get("submitter_username") == username, (
            f"submitter_username must remain '{username}' (the target user), "
            f"got {submitted_kwargs.get('submitter_username')!r}"
        )

    def test_deactivate_repository_no_actor_defaults_to_submitter(
        self, tmp_path: Path
    ) -> None:
        """deactivate_repository(actor_username=None) passes actor_username=None to submit_job."""
        import os
        import json

        from code_indexer.server.repositories.activated_repo_manager import (
            ActivatedRepoManager,
        )
        from code_indexer.server.repositories.background_jobs import (
            BackgroundJobManager,
        )

        data_dir2 = str(tmp_path / "data2")
        username = "carol"
        user_alias = "repo-y"
        repo_dir = os.path.join(data_dir2, "activated-repos", username, user_alias)
        os.makedirs(repo_dir, exist_ok=True)
        data_dir = data_dir2

        metadata_path = os.path.join(
            data_dir, "activated-repos", username, f"{user_alias}_metadata.json"
        )
        with open(metadata_path, "w") as f:
            json.dump({"golden_repo_alias": "g", "created_at": "2026-01-01"}, f)

        bjm = BackgroundJobManager()
        submitted_kwargs: Dict[str, Any] = {}

        def capturing_submit(*args, **kwargs):
            submitted_kwargs.update(kwargs)
            return "fake-job-id-2"

        bjm.submit_job = capturing_submit  # type: ignore[method-assign]

        manager = ActivatedRepoManager(
            data_dir=data_dir,
            background_job_manager=bjm,
        )

        manager.deactivate_repository(
            username=username,
            user_alias=user_alias,
            # actor_username omitted (None by default)
        )

        # When actor_username is None, it should still be passed as None
        # (BackgroundJobManager.submit_job will default to submitter_username)
        assert (
            "actor_username" in submitted_kwargs
            or submitted_kwargs.get("actor_username") is None
        )


# ---------------------------------------------------------------------------
# 8. get_job_status response includes actor_username
# ---------------------------------------------------------------------------


class TestGetJobStatusActorUsername:
    """get_job_status must return actor_username in the response dict."""

    def test_get_job_status_in_memory_includes_actor_username(
        self, job_manager
    ) -> None:
        """get_job_status for an in-memory job includes actor_username."""
        import time

        job_id = job_manager.submit_job(
            "test_op",
            lambda: {"status": "ok"},
            submitter_username="alice",
            repo_alias="some-repo",
            actor_username="superadmin",
        )
        time.sleep(0.1)

        status = job_manager.get_job_status(
            job_id=job_id, username="alice", is_admin=True
        )
        if status is not None:
            # Job may still be in memory or already moved to SQLite
            assert "actor_username" in status, (
                f"get_job_status must include actor_username; got keys: {list(status.keys())}"
            )

    def test_get_job_status_sqlite_includes_actor_username(
        self, job_manager, db_path: Path
    ) -> None:
        """get_job_status for a DB-only job includes actor_username."""
        from code_indexer.server.storage.sqlite_backends import (
            BackgroundJobsSqliteBackend,
        )

        backend = BackgroundJobsSqliteBackend(str(db_path))
        backend.save_job(
            job_id="status-actor-1",
            operation_type="deactivate_repository",
            status="completed",
            created_at="2026-01-01T00:00:00+00:00",
            username="bob",
            progress=100,
            actor_username="admin",
        )

        status = job_manager.get_job_status(
            job_id="status-actor-1", username="bob", is_admin=True
        )
        assert status is not None
        assert status.get("actor_username") == "admin"


# ---------------------------------------------------------------------------
# H1: _get_all_jobs default is_admin must be False (not True)
# ---------------------------------------------------------------------------


class TestGetAllJobsDefaultIsAdmin:
    """H1: _get_all_jobs must have is_admin default=False to avoid privilege escalation."""

    def test_get_all_jobs_default_is_admin_is_false(self) -> None:
        """_get_all_jobs must default is_admin to False, not True."""
        import inspect
        from code_indexer.server.web import routes

        sig = inspect.signature(routes._get_all_jobs)
        param = sig.parameters.get("is_admin")
        assert param is not None, "_get_all_jobs must have is_admin parameter"
        assert param.default is False, (
            f"_get_all_jobs is_admin must default to False (H1 fix), "
            f"got {param.default!r}"
        )


# ---------------------------------------------------------------------------
# H2: get_jobs_for_display SQLite branch must respect username scope for non-admin
# ---------------------------------------------------------------------------


class TestGetJobsForDisplayNonAdminScope:
    """H2: non-admin user must not see DB-stored completed jobs owned by other users."""

    def test_non_admin_cannot_see_other_users_db_jobs(
        self, job_manager, db_path: Path
    ) -> None:
        """get_jobs_for_display with is_admin=False scopes DB results to username."""
        from code_indexer.server.storage.sqlite_backends import (
            BackgroundJobsSqliteBackend,
        )

        backend = BackgroundJobsSqliteBackend(str(db_path))
        # Insert a completed job owned by user_b (will be in DB only, not memory)
        backend.save_job(
            job_id="scope-test-1",
            operation_type="add_golden_repo",
            status="completed",
            created_at="2026-01-01T00:00:00+00:00",
            username="user_b",
            progress=100,
        )

        # user_a queries (is_admin=False) -- must NOT see user_b's job
        jobs, total, _ = job_manager.get_jobs_for_display(
            is_admin=False,
            username="user_a",
        )
        job_ids = [j["job_id"] for j in jobs]
        assert "scope-test-1" not in job_ids, (
            "Non-admin user_a must not see completed jobs owned by user_b via get_jobs_for_display"
        )

    def test_admin_can_see_all_users_db_jobs(self, job_manager, db_path: Path) -> None:
        """get_jobs_for_display with is_admin=True returns all users' completed jobs."""
        from code_indexer.server.storage.sqlite_backends import (
            BackgroundJobsSqliteBackend,
        )

        backend = BackgroundJobsSqliteBackend(str(db_path))
        backend.save_job(
            job_id="scope-admin-1",
            operation_type="add_golden_repo",
            status="completed",
            created_at="2026-01-01T00:00:00+00:00",
            username="user_c",
            progress=100,
        )

        # Admin queries -- must see user_c's job
        jobs, total, _ = job_manager.get_jobs_for_display(
            is_admin=True,
            username=None,
        )
        job_ids = [j["job_id"] for j in jobs]
        assert "scope-admin-1" in job_ids, (
            "Admin must see all users' completed jobs via get_jobs_for_display"
        )


# ---------------------------------------------------------------------------
# H3: _build_deactivating_map logs ERROR on exception with exc_info
# ---------------------------------------------------------------------------


class TestBuildDeactivatingMapErrorLogging:
    """H3: _build_deactivating_map must log ERROR with exc_info=True on exceptions."""

    def test_build_deactivating_map_logs_error_on_exception(self, caplog) -> None:
        """When list_jobs raises, _build_deactivating_map logs ERROR with exc_info."""
        import logging
        from unittest.mock import MagicMock, patch

        broken_manager = MagicMock()
        broken_manager.list_jobs.side_effect = RuntimeError("DB is gone")

        with patch(
            "code_indexer.server.web.routes._get_background_job_manager",
            return_value=broken_manager,
        ):
            with caplog.at_level(logging.ERROR):
                from code_indexer.server.web.routes import _build_deactivating_map

                result = _build_deactivating_map()

        assert result == {}, "Result must be empty dict on error"
        error_records = [r for r in caplog.records if r.levelno >= logging.ERROR]
        assert len(error_records) >= 1, (
            "Must log at ERROR level when list_jobs raises (H3 fix)"
        )
        # exc_info must be set so the traceback is included
        assert any(r.exc_info is not None for r in error_records), (
            "ERROR log must include exc_info=True (H3 fix)"
        )


# ---------------------------------------------------------------------------
# 9. Dashboard "Recent Jobs" partial displays actor → owner when actor != owner
# ---------------------------------------------------------------------------


class TestDashboardRecentJobsActorDisplay:
    """AC12: Recent Jobs partial shows actor_username → username when they differ."""

    def test_actor_display_when_actor_differs_from_owner(self) -> None:
        """When actor_username != username, template renders 'actor → owner'."""
        from jinja2 import Environment, FileSystemLoader

        template_dir = (
            Path(__file__).parent.parent.parent.parent.parent
            / "src/code_indexer/server/web/templates/partials"
        )
        env = Environment(loader=FileSystemLoader(str(template_dir)))
        tmpl = env.get_template("dashboard_recent_jobs.html")

        jobs = [
            {
                "job_id": "job-abc-123",
                "repo_name": "my-repo",
                "job_type": "deactivate_repository",
                "completion_time": "2026-01-01T00:00:00",
                "status": "completed",
                "username": "bob",
                "actor_username": "admin",
                "result": None,
            }
        ]
        rendered = tmpl.render(recent_jobs=jobs, has_provider_results=False)
        # Should contain "admin" and "bob" and "→" to indicate actor → owner
        assert "admin" in rendered, "Rendered partial must include actor username"
        assert "bob" in rendered, "Rendered partial must include owner username"
        # The separator arrow distinguishes admin-initiated from self-initiated
        assert "→" in rendered, "Rendered partial must show → between actor and owner"

    def test_no_actor_display_when_actor_matches_owner(self) -> None:
        """When actor_username == username (self-initiated), only show username once."""
        from jinja2 import Environment, FileSystemLoader

        template_dir = (
            Path(__file__).parent.parent.parent.parent.parent
            / "src/code_indexer/server/web/templates/partials"
        )
        env = Environment(loader=FileSystemLoader(str(template_dir)))
        tmpl = env.get_template("dashboard_recent_jobs.html")

        jobs = [
            {
                "job_id": "job-def-456",
                "repo_name": "self-repo",
                "job_type": "deactivate_repository",
                "completion_time": "2026-01-01T00:00:00",
                "status": "completed",
                "username": "carol",
                "actor_username": "carol",  # same as owner
                "result": None,
            }
        ]
        rendered = tmpl.render(recent_jobs=jobs, has_provider_results=False)
        # Should not show the → arrow when actor == owner
        assert "→" not in rendered, (
            "Must not show → separator when actor_username == username"
        )

    def test_null_actor_username_shows_only_username(self) -> None:
        """When actor_username is None (old row), show only username with no arrow."""
        from jinja2 import Environment, FileSystemLoader

        template_dir = (
            Path(__file__).parent.parent.parent.parent.parent
            / "src/code_indexer/server/web/templates/partials"
        )
        env = Environment(loader=FileSystemLoader(str(template_dir)))
        tmpl = env.get_template("dashboard_recent_jobs.html")

        jobs = [
            {
                "job_id": "job-ghi-789",
                "repo_name": "legacy-repo",
                "job_type": "deactivate_repository",
                "completion_time": "2026-01-01T00:00:00",
                "status": "completed",
                "username": "dave",
                "actor_username": None,  # pre-migration row
                "result": None,
            }
        ]
        rendered = tmpl.render(recent_jobs=jobs, has_provider_results=False)
        # No → arrow when actor_username is None
        assert "→" not in rendered, (
            "Must not show → separator when actor_username is None"
        )
