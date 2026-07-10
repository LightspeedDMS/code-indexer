"""
Unit tests for Bug #1349 (follow-up to Bug #1345, itself a follow-up to
Bug #1342, shipped in v11.36.0/v11.37.0).

Bug #1345 added a defense-in-depth cleanup in `_do_activate_repository`'s
clone-phase `except ActivatedRepoError:` handler: a single
`if os.path.exists(activated_repo_path): shutil.rmtree(...)` check. On
staging (CoW Storage Daemon clone backend over NFS), this single-instant
check still missed a real orphan: the daemon can still be asynchronously
materializing the clone directory on the NFS client mount a beat AFTER
this check runs and returns False, so neither the WARNING log nor the
rmtree fire, and the fully-materialized clone becomes a permanent,
unregistered orphan on shared storage.

The fix replaces the single-shot check with a short, BOUNDED retry loop
(fixed iteration count, fixed sleep -- NOT a timeout on the indexing job
itself, see the Bug #1218 invariant) that gives late/async materialization
a brief grace period to settle before cleanup gives up. The unconditional
first `shutil.rmtree(..., ignore_errors=True)` call is free/safe even when
the path does not exist yet.

Mocking policy (anti-mock): only the clone_backend and index_manager seams
are stubbed (same seam `test_activated_repo_manager_cancel_1342.py` and
`..._1345_1346.py` use). The method under test, `_do_activate_repository`,
runs for real -- including real exception handling and real filesystem
operations against a real temp directory. The "async NFS materialization"
scenario is simulated with a real background thread that ACTUALLY creates
the directory on disk a short time after the clone call raises, rather
than mocking `os.path.exists` -- this exercises the real retry loop timing
instead of a stubbed existence check.
"""

import os
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from src.code_indexer.server.repositories.activated_repo_manager import (
    ActivatedRepoError,
    ActivatedRepoManager,
)
from src.code_indexer.server.repositories.golden_repo_manager import GoldenRepo

# NOTE: imported WITHOUT the "src." prefix -- see the identical comment in
# test_activated_repo_manager_cancel_1345_1346.py for why this matters for
# isinstance() checks in the production code.
from code_indexer.server.utils.cancellable_subprocess import (
    SubprocessCancelledError,
)
from src.code_indexer.server.utils.config_manager import ServerResourceConfig


@pytest.fixture
def temp_data_dir():
    with tempfile.TemporaryDirectory() as temp_dir:
        yield temp_dir


@pytest.fixture
def golden_repo_manager_mock():
    mock = MagicMock()
    golden_repo = GoldenRepo(
        alias="test-repo",
        repo_url="https://github.com/example/test-repo.git",
        default_branch="main",
        clone_path="/path/to/golden/test-repo",
        created_at=datetime.now(timezone.utc).isoformat(),
    )
    golden_repos_dict = {"test-repo": golden_repo}
    mock.golden_repos = golden_repos_dict
    mock.get_golden_repo.side_effect = lambda alias: golden_repos_dict.get(alias)
    mock.get_actual_repo_path.return_value = "/path/to/golden/test-repo"
    mock.resource_config = ServerResourceConfig()
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
def mock_index_manager():
    return MagicMock()


@pytest.fixture
def activated_repo_manager(
    temp_data_dir,
    golden_repo_manager_mock,
    background_job_manager_mock,
    mock_clone_backend,
    mock_index_manager,
):
    return ActivatedRepoManager(
        data_dir=temp_data_dir,
        golden_repo_manager=golden_repo_manager_mock,
        background_job_manager=background_job_manager_mock,
        clone_backend=mock_clone_backend,
        index_manager=mock_index_manager,
    )


def _patch_committer():
    return patch(
        "src.code_indexer.server.repositories.activated_repo_manager"
        ".CommitterResolutionService"
    )


# Bug #1350: test-only override for the retry loop constants, kept tiny so
# the "directory never appears" scenario stays fast even though the real
# production bound was widened to ~12s (see the constants lock-in test).
_TEST_FAST_RETRY_ATTEMPTS = 3
_TEST_FAST_RETRY_SLEEP_SECONDS = 0.01
_TEST_FAST_LOOP_MAX_ELAPSED_SECONDS = 5.0


def _patch_fast_retry_constants():
    return patch.multiple(
        "src.code_indexer.server.repositories.activated_repo_manager",
        _ORPHAN_CLEANUP_RETRY_ATTEMPTS=_TEST_FAST_RETRY_ATTEMPTS,
        _ORPHAN_CLEANUP_RETRY_SLEEP_SECONDS=_TEST_FAST_RETRY_SLEEP_SECONDS,
    )


def _find_exhaustion_warnings(caplog_records, user_alias):
    return [
        record
        for record in caplog_records
        if record.levelname == "WARNING"
        and "exhausted" in record.message
        and user_alias in record.message
    ]


class TestClonePhaseOrphanCleanupBoundedRetry1349:
    def test_late_nfs_materialization_is_still_cleaned_up_by_bounded_retry(
        self, activated_repo_manager, mock_clone_backend
    ):
        """A clone-phase cancellation whose partial directory is created by
        a REAL background thread ~0.15s AFTER the clone call raises (
        simulating the CoW Storage Daemon finishing async materialization
        on the NFS client mount after the cancellation exception has
        already propagated) must still be removed by the bounded retry
        loop -- not just a single, instant os.path.exists() check."""
        activated_repo_path = os.path.join(
            activated_repo_manager.activated_repos_dir,
            "testuser",
            "my-activated-repo",
        )

        timer_holder = {}

        def _fake_clone(source_path, dest_path, **kwargs):
            def _materialize_late():
                os.makedirs(dest_path, exist_ok=True)
                (Path(dest_path) / "late-materialized.txt").write_text("late")

            timer = threading.Timer(0.15, _materialize_late)
            timer.daemon = True
            timer.start()
            timer_holder["timer"] = timer
            raise SubprocessCancelledError("clone cancelled")

        mock_clone_backend.create_clone_at_path.side_effect = _fake_clone

        with _patch_committer():
            with pytest.raises(ActivatedRepoError):
                activated_repo_manager._do_activate_repository(
                    username="testuser",
                    golden_repo_alias="test-repo",
                    branch_name="main",  # default branch: isolates clone phase
                    user_alias="my-activated-repo",
                    cancel_check=lambda: True,
                )

        # Make sure the background timer thread has DEFINITELY fired (its
        # own materialization) before asserting the final state -- this is
        # what proves the fix actively retries DURING the call, rather
        # than merely winning an accidental race in this test: without the
        # fix, the single check already ran (and found nothing) before the
        # timer fires, and nothing later re-checks, so the directory would
        # remain as a permanent orphan after the timer fires.
        timer_holder["timer"].join(timeout=2.0)
        assert not os.path.exists(activated_repo_path), (
            "orphaned clone directory that materializes AFTER the clone "
            "call raises (simulating async NFS-backed daemon completion) "
            "must still be removed by the bounded retry loop"
        )

    def test_immediate_directory_is_removed_without_extra_sleep_attempts(
        self, activated_repo_manager, tmp_path
    ):
        """Regression guard for the fast, common case: when the partial
        directory is already present the moment
        `_cleanup_orphaned_clone_after_failure` runs (the pre-#1349 case --
        e.g. the inner `_clone_with_copy_on_write` cleanup already raced
        ahead and missed it, or a purely local/non-NFS clone backend where
        there is no visibility lag at all), it must be removed on the
        first, unconditional rmtree attempt with NO retry sleeps -- the
        bounded retry loop must not regress the common, non-racy path
        with needless latency.

        This exercises the cleanup helper directly (rather than the full
        `_do_activate_repository` flow) because, in the full flow,
        `_clone_with_copy_on_write`'s own inner exception handler removes
        a synchronously-created directory before this outer helper ever
        runs -- making it impossible to observe "already present at the
        outer check" through the full flow without re-mocking
        `os.path.exists` (the exact seam #1345's test already covers).
        Calling the helper directly isolates its own first-check/no-sleep
        contract precisely.
        """
        activated_repo_path = str(tmp_path / "existing-orphan")
        os.makedirs(activated_repo_path, exist_ok=True)
        (Path(activated_repo_path) / "partial-file.txt").write_text("partial")

        with patch(
            "src.code_indexer.server.repositories.activated_repo_manager.time.sleep"
        ) as mock_sleep:
            activated_repo_manager._cleanup_orphaned_clone_after_failure(
                activated_repo_path, "my-activated-repo"
            )

        assert not os.path.exists(activated_repo_path), (
            "orphan present at first check must still be removed"
        )
        mock_sleep.assert_not_called()

    def test_directory_never_appears_no_error_no_infinite_loop(
        self, activated_repo_manager, mock_clone_backend, caplog
    ):
        """When the clone never materializes anything on disk at all (a
        pure in-memory cancellation, e.g. before any bytes were written),
        the bounded retry loop must terminate promptly with no error, no
        unbounded looping, and (Bug #1350) must now log a distinct
        exhaustion WARNING instead of staying completely silent.

        The retry constants are patched down to tiny test-only values
        (see `_patch_fast_retry_constants`) so this scenario stays fast
        even though production widened the real bound to ~12s -- the
        loop's termination/logging behavior is what's under test here,
        not the exact production timing (covered separately below).
        """
        activated_repo_path = os.path.join(
            activated_repo_manager.activated_repos_dir,
            "testuser",
            "my-activated-repo",
        )

        mock_clone_backend.create_clone_at_path.side_effect = SubprocessCancelledError(
            "clone cancelled"
        )

        start = time.monotonic()
        with (
            _patch_committer(),
            _patch_fast_retry_constants(),
            caplog.at_level("WARNING"),
        ):
            with pytest.raises(ActivatedRepoError):
                activated_repo_manager._do_activate_repository(
                    username="testuser",
                    golden_repo_alias="test-repo",
                    branch_name="main",
                    user_alias="my-activated-repo",
                    cancel_check=lambda: True,
                )
        elapsed = time.monotonic() - start

        assert not os.path.exists(activated_repo_path)
        assert elapsed < _TEST_FAST_LOOP_MAX_ELAPSED_SECONDS, (
            f"cleanup retry loop took {elapsed:.2f}s -- expected a bounded, "
            f"fast exit when the directory never appears"
        )

        exhaustion_warnings = _find_exhaustion_warnings(
            caplog.records, "my-activated-repo"
        )
        assert exhaustion_warnings, (
            "expected a WARNING logging that clone-phase cleanup exhausted "
            "its retry attempts without observing/removing the directory, "
            f"got log records: {[r.message for r in caplog.records]}"
        )

    def test_genuine_non_cancellation_failure_still_gets_same_cleanup(
        self, activated_repo_manager, mock_clone_backend
    ):
        """A genuine (non-cancellation) clone failure whose partial
        directory materializes late must receive the identical bounded
        retry cleanup -- this is not cancellation-specific."""
        activated_repo_path = os.path.join(
            activated_repo_manager.activated_repos_dir,
            "testuser",
            "my-activated-repo",
        )

        timer_holder = {}

        def _fake_clone(source_path, dest_path, **kwargs):
            def _materialize_late():
                os.makedirs(dest_path, exist_ok=True)
                (Path(dest_path) / "late-materialized.txt").write_text("late")

            timer = threading.Timer(0.15, _materialize_late)
            timer.daemon = True
            timer.start()
            timer_holder["timer"] = timer
            raise RuntimeError("disk full")

        mock_clone_backend.create_clone_at_path.side_effect = _fake_clone

        with _patch_committer():
            with pytest.raises(ActivatedRepoError):
                activated_repo_manager._do_activate_repository(
                    username="testuser",
                    golden_repo_alias="test-repo",
                    branch_name="main",
                    user_alias="my-activated-repo",
                    cancel_check=lambda: False,
                )

        timer_holder["timer"].join(timeout=2.0)
        assert not os.path.exists(activated_repo_path), (
            "genuine (non-cancel) clone failures must also get the bounded "
            "retry cleanup, not just user cancellations"
        )


# Bug #1350: staging reproduced 3/3 permanent orphans because the #1349
# bounded retry window (~1.2s worst case) was too short for real
# CoW-daemon/NFS materialization lag. These tests lock in the widened
# window (production constants, not patched) and its new exhaustion
# diagnostic.
_OLD_1349_WORST_CASE_BOUND_SECONDS = 1.2
_WIDENED_WINDOW_MIN_TOTAL_SECONDS = 10.0
_WIDENED_WINDOW_MAX_TOTAL_SECONDS = 15.0


class TestClonePhaseOrphanCleanupWidenedWindow1350:
    def test_delay_beyond_old_bound_is_still_cleaned_up_by_widened_window(
        self, activated_repo_manager, mock_clone_backend
    ):
        """A clone-phase cancellation whose partial directory is created
        by a REAL background thread ~3s after the clone call raises --
        well beyond the OLD #1349 worst-case bound of ~1.2s, but safely
        inside the new Bug #1350 widened bound -- must still be removed.
        This is the exact staging failure mode: the async CoW-daemon/NFS
        materialization simply took longer than the old window allowed."""
        activated_repo_path = os.path.join(
            activated_repo_manager.activated_repos_dir,
            "testuser",
            "my-activated-repo",
        )

        timer_holder = {}
        materialize_delay_seconds = _OLD_1349_WORST_CASE_BOUND_SECONDS + 1.8

        def _fake_clone(source_path, dest_path, **kwargs):
            def _materialize_late():
                os.makedirs(dest_path, exist_ok=True)
                (Path(dest_path) / "late-materialized.txt").write_text("late")

            timer = threading.Timer(materialize_delay_seconds, _materialize_late)
            timer.daemon = True
            timer.start()
            timer_holder["timer"] = timer
            raise SubprocessCancelledError("clone cancelled")

        mock_clone_backend.create_clone_at_path.side_effect = _fake_clone

        with _patch_committer():
            with pytest.raises(ActivatedRepoError):
                activated_repo_manager._do_activate_repository(
                    username="testuser",
                    golden_repo_alias="test-repo",
                    branch_name="main",
                    user_alias="my-activated-repo",
                    cancel_check=lambda: True,
                )

        timer_holder["timer"].join(timeout=15.0)
        assert not os.path.exists(activated_repo_path), (
            f"a directory materializing {materialize_delay_seconds:.1f}s "
            "late (beyond the old ~1.2s bound) must be removed by the "
            "widened Bug #1350 retry window"
        )

    def test_retry_constants_total_to_widened_bound(self):
        """Lock in the widened bound: (attempts - 1) * sleep_seconds must
        fall in the 10-15s range the issue asked for, replacing the old
        ~1.2s worst case that staging proved too short."""
        from src.code_indexer.server.repositories import activated_repo_manager as m

        total_bound_seconds = (
            m._ORPHAN_CLEANUP_RETRY_ATTEMPTS - 1
        ) * m._ORPHAN_CLEANUP_RETRY_SLEEP_SECONDS

        assert _WIDENED_WINDOW_MIN_TOTAL_SECONDS <= total_bound_seconds
        assert total_bound_seconds <= _WIDENED_WINDOW_MAX_TOTAL_SECONDS
