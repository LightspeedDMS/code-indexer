"""
Unit tests for Bug #1393 fix: ActivatedRepoManager's CoW clone coordinates
with RefreshScheduler's WriteLockManager and JobTracker.

Bug #1393: `_clone_with_copy_on_write` ran a `cp --reflink=auto -a` CoW clone
of a golden repo's source tree with ZERO coordination against a concurrently
running `global_repo_refresh` on the same golden repo -- a production
incident showed the clone reading a non-atomic, concurrently-mutated
snapshot and blowing its cow_clone_timeout.

Fix has two directions:
1. Activation becomes a WriteLockManager HOLDER (owner_name=
   "activation_clone") around the clone step so any refresh that starts
   AFTER activation begins yields via its existing is_write_locked() check
   -- mirrors golden_repo_manager.change_branch's identical use of the same
   lock for the same kind of "touch the golden repo source tree" operation.
2. Activation fails fast (ActivatedRepoError) when a global_repo_refresh is
   ALREADY in flight, via RefreshScheduler.check_refresh_not_in_progress()
   (JobTracker-backed -- the only cluster-visible "currently executing"
   signal, since a running refresh never itself holds the write lock).

Mocking policy (anti-mock): golden_repo_manager and clone_backend are the
established test doubles already used by the sibling
test_activated_repo_manager_cancel_134*.py suite (parameter-wiring/cleanup
verification, not the code under test). The RefreshScheduler,
WriteLockManager, and JobTracker are ALL REAL -- backed by a real temp
directory and a real SQLite background_jobs table -- because these are the
storage/coordination primitives Bug #1393 is actually about; mocking them
would prove nothing about the race being fixed.

Import note: every production symbol in this file is imported via the
plain `code_indexer...` path (never `src.code_indexer...`). Mixing the two
prefixes makes Python load two separate copies of shared modules like
job_tracker.py, so an exception raised by one copy's class will not match
an `except SameName` clause referencing the other copy's class -- exactly
the kind of gotcha documented in test_activated_repo_manager_cancel_1345_1346.py.
"""

import os
import sqlite3
import tempfile
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from code_indexer.server.repositories.activated_repo_manager import (
    ActivatedRepoError,
    ActivatedRepoManager,
)
from code_indexer.server.repositories.golden_repo_manager import GoldenRepo
from code_indexer.server.utils.config_manager import ServerResourceConfig
from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.config import ConfigManager
from code_indexer.server.services.job_tracker import JobTracker


_BACKGROUND_JOBS_DDL = """
    CREATE TABLE IF NOT EXISTS background_jobs (
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
        repo_alias TEXT,
        resolution_attempts INTEGER DEFAULT 0,
        progress_info TEXT,
        metadata TEXT,
        actor_username TEXT
    )
"""


@pytest.fixture
def temp_data_dir():
    with tempfile.TemporaryDirectory() as temp_dir:
        yield temp_dir


@pytest.fixture
def job_tracker_real(tmp_path):
    """Real JobTracker backed by a real SQLite background_jobs table."""
    db_path = str(tmp_path / "tracker.db")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(_BACKGROUND_JOBS_DDL)
        conn.commit()
    finally:
        conn.close()
    return JobTracker(db_path)


@pytest.fixture
def real_refresh_scheduler(tmp_path, job_tracker_real):
    """Real RefreshScheduler with a real WriteLockManager (file-based locks
    under a real temp golden_repos_dir) and a real JobTracker."""
    golden_repos_dir = tmp_path / "golden-repos"
    golden_repos_dir.mkdir(parents=True, exist_ok=True)
    config_mgr = ConfigManager(tmp_path / ".code-indexer" / "config.json")
    query_tracker = QueryTracker()
    cleanup_manager = CleanupManager(query_tracker)
    return RefreshScheduler(
        golden_repos_dir=str(golden_repos_dir),
        config_source=config_mgr,
        query_tracker=query_tracker,
        cleanup_manager=cleanup_manager,
        job_tracker=job_tracker_real,
    )


@pytest.fixture
def golden_repo_manager_mock(real_refresh_scheduler):
    """Mock golden repo manager wired to a REAL RefreshScheduler."""
    mock = MagicMock()
    golden_repo = GoldenRepo(
        alias="evolution",
        repo_url="https://github.com/example/evolution.git",
        default_branch="main",
        clone_path="/path/to/golden/evolution",
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    golden_repos_dict = {"evolution": golden_repo}
    mock.golden_repos = golden_repos_dict
    mock.get_golden_repo.side_effect = lambda alias: golden_repos_dict.get(alias)
    mock.get_actual_repo_path.return_value = "/path/to/golden/evolution"
    mock.resource_config = ServerResourceConfig()
    mock._refresh_scheduler = real_refresh_scheduler
    return mock


@pytest.fixture
def background_job_manager_mock():
    mock = MagicMock()
    mock.submit_job.return_value = "job-123"
    return mock


@pytest.fixture
def mock_clone_backend():
    backend = MagicMock()
    backend.create_clone_at_path.return_value = "/dest/path"
    return backend


@pytest.fixture
def activated_repo_manager(
    temp_data_dir,
    golden_repo_manager_mock,
    background_job_manager_mock,
    mock_clone_backend,
):
    return ActivatedRepoManager(
        data_dir=temp_data_dir,
        golden_repo_manager=golden_repo_manager_mock,
        background_job_manager=background_job_manager_mock,
        clone_backend=mock_clone_backend,
    )


class TestActivationAcquiresWriteLockAroundClone:
    def test_lock_is_held_during_clone_and_released_after_success(
        self,
        activated_repo_manager,
        real_refresh_scheduler,
        mock_clone_backend,
        tmp_path,
    ):
        """The write lock must be HELD while create_clone_at_path runs, and
        released once _clone_with_copy_on_write returns successfully."""
        dest_path = tmp_path / "dest"

        lock_state_during_clone = {}

        def _fake_clone(source_path, dest_path_arg, **kwargs):
            lock_state_during_clone["held"] = real_refresh_scheduler.is_write_locked(
                "evolution"
            )
            os.makedirs(dest_path_arg, exist_ok=True)
            return dest_path_arg

        mock_clone_backend.create_clone_at_path.side_effect = _fake_clone

        assert real_refresh_scheduler.is_write_locked("evolution") is False

        success = activated_repo_manager._clone_with_copy_on_write(
            "/src/path", str(dest_path), golden_repo_alias="evolution"
        )

        assert success is True
        assert lock_state_during_clone["held"] is True, (
            "write lock must be held DURING the clone call"
        )
        assert real_refresh_scheduler.is_write_locked("evolution") is False, (
            "write lock must be released after a successful clone"
        )

    def test_lock_is_released_when_clone_raises(
        self,
        activated_repo_manager,
        real_refresh_scheduler,
        mock_clone_backend,
        tmp_path,
    ):
        """The write lock must be released in a finally even when the clone
        backend raises -- no lock leak on failure."""
        dest_path = tmp_path / "dest"
        mock_clone_backend.create_clone_at_path.side_effect = RuntimeError(
            "simulated clone failure"
        )

        with pytest.raises(ActivatedRepoError):
            activated_repo_manager._clone_with_copy_on_write(
                "/src/path", str(dest_path), golden_repo_alias="evolution"
            )

        assert real_refresh_scheduler.is_write_locked("evolution") is False, (
            "write lock must be released even when the clone fails"
        )


class TestActivationRefusesWhenLockAlreadyHeld:
    def test_raises_and_never_clones_when_lock_held_by_external_writer(
        self,
        activated_repo_manager,
        real_refresh_scheduler,
        mock_clone_backend,
        tmp_path,
    ):
        """When another writer (e.g. DependencyMapService, branch_change)
        already holds the lock, activation must fail fast WITHOUT cloning
        and must NOT release a lock it never acquired."""
        acquired = real_refresh_scheduler.acquire_write_lock(
            "evolution", owner_name="external_writer"
        )
        assert acquired is True

        dest_path = tmp_path / "dest"

        with pytest.raises(ActivatedRepoError):
            activated_repo_manager._clone_with_copy_on_write(
                "/src/path", str(dest_path), golden_repo_alias="evolution"
            )

        mock_clone_backend.create_clone_at_path.assert_not_called()

        lock_info = real_refresh_scheduler.write_lock_manager.get_lock_info("evolution")
        assert lock_info is not None, "external_writer's lock must remain held"
        assert lock_info["owner"] == "external_writer"


class TestActivationFailsFastOnInFlightRefresh:
    def test_raises_and_never_clones_when_refresh_job_in_flight(
        self,
        activated_repo_manager,
        real_refresh_scheduler,
        job_tracker_real,
        mock_clone_backend,
        tmp_path,
    ):
        """A refresh that is ALREADY executing (JobTracker-registered,
        Bug #935 convention) must cause activation to fail fast -- the
        write lock alone cannot see this because a running refresh never
        holds it itself."""
        job_tracker_real.register_job(
            "refresh-evolution-global",
            operation_type="global_repo_refresh",
            username="system",
            repo_alias="evolution-global",
        )
        job_tracker_real.update_status("refresh-evolution-global", status="running")

        dest_path = tmp_path / "dest"

        with pytest.raises(
            ActivatedRepoError, match="(?is)evolution.*refresh|refresh.*evolution"
        ):
            activated_repo_manager._clone_with_copy_on_write(
                "/src/path", str(dest_path), golden_repo_alias="evolution"
            )

        mock_clone_backend.create_clone_at_path.assert_not_called()
        assert real_refresh_scheduler.is_write_locked("evolution") is False, (
            "no lock should be left behind -- the JobTracker check runs "
            "BEFORE lock acquisition"
        )


class TestBackwardCompatibility:
    def test_no_golden_repo_alias_skips_coordination(
        self,
        activated_repo_manager,
        real_refresh_scheduler,
        mock_clone_backend,
        tmp_path,
    ):
        """Legacy/test call sites that omit golden_repo_alias (the pre-#1393
        default) must not touch lock/JobTracker coordination at all -- this
        is what keeps the ~17 pre-existing _clone_with_copy_on_write test
        files (which never pass this kwarg) unaffected."""
        from unittest.mock import patch

        dest_path = tmp_path / "dest"
        mock_clone_backend.create_clone_at_path.return_value = str(dest_path)
        dest_path.mkdir()

        with (
            patch.object(
                real_refresh_scheduler,
                "acquire_write_lock",
                wraps=real_refresh_scheduler.acquire_write_lock,
            ) as spy_acquire,
            patch.object(
                real_refresh_scheduler,
                "check_refresh_not_in_progress",
                wraps=real_refresh_scheduler.check_refresh_not_in_progress,
            ) as spy_check,
        ):
            success = activated_repo_manager._clone_with_copy_on_write(
                "/src/path", str(dest_path)
            )

        assert success is True
        spy_acquire.assert_not_called()
        spy_check.assert_not_called()

    def test_no_scheduler_wired_still_clones(
        self,
        temp_data_dir,
        golden_repo_manager_mock,
        background_job_manager_mock,
        mock_clone_backend,
        tmp_path,
    ):
        """CLI/solo mode (golden_repo_manager._refresh_scheduler is None)
        must still clone successfully -- coordination is skipped, not
        required, when there is no scheduler to coordinate with."""
        golden_repo_manager_mock._refresh_scheduler = None
        manager = ActivatedRepoManager(
            data_dir=temp_data_dir,
            golden_repo_manager=golden_repo_manager_mock,
            background_job_manager=background_job_manager_mock,
            clone_backend=mock_clone_backend,
        )

        dest_path = tmp_path / "dest"
        mock_clone_backend.create_clone_at_path.return_value = str(dest_path)
        dest_path.mkdir()

        success = manager._clone_with_copy_on_write(
            "/src/path", str(dest_path), golden_repo_alias="evolution"
        )

        assert success is True


class TestSingleRepoPathParticipatesInLockCoordination:
    def test_do_activate_repository_engages_write_lock_during_clone(
        self,
        activated_repo_manager,
        real_refresh_scheduler,
        mock_clone_backend,
    ):
        """The real single-repo activation call site (_do_activate_repository)
        must pass golden_repo_alias through to _clone_with_copy_on_write --
        proven by observing the real scheduler's write lock actually engage
        during the clone step."""
        from unittest.mock import patch

        lock_state_during_clone = {}

        def _fake_clone(source_path, dest_path_arg, **kwargs):
            lock_state_during_clone["held"] = real_refresh_scheduler.is_write_locked(
                "evolution"
            )
            os.makedirs(dest_path_arg, exist_ok=True)
            return dest_path_arg

        mock_clone_backend.create_clone_at_path.side_effect = _fake_clone

        with patch(
            "code_indexer.server.repositories.activated_repo_manager"
            ".CommitterResolutionService"
        ) as mock_committer_cls:
            mock_committer_cls.return_value.resolve_committer_email.return_value = (
                "",
                None,
            )
            activated_repo_manager._do_activate_repository(
                username="testuser",
                golden_repo_alias="evolution",
                branch_name="main",  # matches golden_repo default_branch
                user_alias="my-activated-repo",
            )

        assert lock_state_during_clone.get("held") is True, (
            "the single-repo activation path must pass golden_repo_alias "
            "through so the write lock actually engages during the clone"
        )
        assert real_refresh_scheduler.is_write_locked("evolution") is False, (
            "lock must be released after activation completes"
        )


class TestCompositeLoopParticipatesInLockCoordination:
    def test_do_activate_composite_repository_engages_write_lock_for_each_component(
        self,
        activated_repo_manager,
        golden_repo_manager_mock,
        real_refresh_scheduler,
        mock_clone_backend,
    ):
        """The composite-activation loop must pass golden_repo_alias through
        for EVERY component repo, not just the single-repo path -- proven by
        observing the real scheduler's write lock engage per-component."""
        phoenix_repo = GoldenRepo(
            alias="phoenix",
            repo_url="https://github.com/example/phoenix.git",
            default_branch="main",
            clone_path="/path/to/golden/phoenix",
            created_at=datetime.now(timezone.utc).isoformat(),
        )
        golden_repo_manager_mock.golden_repos["phoenix"] = phoenix_repo

        lock_state_by_alias: dict = {}

        def _fake_clone(source_path, dest_path_arg, **kwargs):
            alias = os.path.basename(dest_path_arg.rstrip("/"))
            lock_state_by_alias[alias] = real_refresh_scheduler.is_write_locked(alias)
            os.makedirs(dest_path_arg, exist_ok=True)
            return dest_path_arg

        mock_clone_backend.create_clone_at_path.side_effect = _fake_clone

        result = activated_repo_manager._do_activate_composite_repository(
            username="testuser",
            golden_repo_aliases=["evolution", "phoenix"],
            user_alias="my-composite",
        )

        assert result["success"] is True
        assert lock_state_by_alias == {"evolution": True, "phoenix": True}, (
            "the write lock must engage for EVERY component alias during "
            "its own clone call"
        )
        assert real_refresh_scheduler.is_write_locked("evolution") is False
        assert real_refresh_scheduler.is_write_locked("phoenix") is False
