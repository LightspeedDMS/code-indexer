"""Deactivation's bounded QueryTracker-refcount-aware drain before purging
the trashed clone (Story #1458 AC13).

`_do_deactivate_single` must call `wait_for_activated_repo_query_drain`
with the ORIGINAL pre-rename activated-repo path (`repo_dir` = os.path.join
(activated_repos_dir, username, user_alias) -- the SAME key format
`_search_activated_repo`'s QueryTracker wiring uses) BEFORE
`_safe_purge_trash_entry` physically deletes the clone's bytes.

Real ActivatedRepoManager, real filesystem, real BackgroundJobManager-
captured worker invocation (mirroring test_activated_repo_manager_1458_
activation_id.py's established pattern) -- only the drain function itself
is spied on (its OWN wait/timeout mechanism is already fully covered by
test_deactivation_query_drain_1458.py), to prove the call site is wired
with the correct key BEFORE the destructive purge.
"""

import json
import os
import tempfile
from datetime import datetime, timezone
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.global_repos.query_tracker import QueryTracker
from src.code_indexer.server.repositories.activated_repo_manager import (
    ActivatedRepoManager,
)
from src.code_indexer.server.repositories.golden_repo_manager import GoldenRepo


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
    mock.get_golden_repo.return_value = golden_repo
    return mock


@pytest.fixture
def background_job_manager_mock():
    mock = MagicMock()
    mock.submit_job.return_value = "job-123"
    return mock


class TestDeactivationDrainWiring:
    def test_do_deactivate_single_drains_before_purge_with_original_path_key(
        self, temp_data_dir, golden_repo_manager_mock, background_job_manager_mock
    ):
        manager = ActivatedRepoManager(
            data_dir=temp_data_dir,
            golden_repo_manager=golden_repo_manager_mock,
            background_job_manager=background_job_manager_mock,
        )
        real_query_tracker = QueryTracker()
        manager.set_query_tracker(real_query_tracker)

        username = "testuser"
        user_alias = "my-repo"
        user_dir = os.path.join(manager.activated_repos_dir, username)
        os.makedirs(user_dir, exist_ok=True)
        repo_dir = os.path.join(user_dir, user_alias)
        os.makedirs(repo_dir, exist_ok=True)
        with open(os.path.join(repo_dir, "marker.txt"), "w") as f:
            f.write("x")

        repo_data = {
            "user_alias": user_alias,
            "golden_repo_alias": "test-repo",
            "current_branch": "main",
            "activated_at": "2026-01-01T00:00:00+00:00",
            "last_accessed": "2026-01-01T00:00:00+00:00",
        }
        with open(os.path.join(user_dir, f"{user_alias}_metadata.json"), "w") as f:
            json.dump(repo_data, f)

        expected_key = os.path.join(manager.activated_repos_dir, username, user_alias)

        drain_calls = []
        activated_repo_manager_module = __import__(
            "src.code_indexer.server.repositories.activated_repo_manager",
            fromlist=["wait_for_activated_repo_query_drain"],
        )
        original_drain = (
            activated_repo_manager_module.wait_for_activated_repo_query_drain
        )

        def spy_drain(query_tracker, refcount_key, **kwargs):
            drain_calls.append((query_tracker, refcount_key))
            return original_drain(query_tracker, refcount_key, **kwargs)

        with patch(
            "src.code_indexer.server.repositories.activated_repo_manager."
            "wait_for_activated_repo_query_drain",
            spy_drain,
        ):
            manager.deactivate_repository(username, user_alias)

            call_args = background_job_manager_mock.submit_job.call_args
            worker_fn = call_args[0][1]
            worker_fn(username=username, user_alias=user_alias)

        assert drain_calls == [(real_query_tracker, expected_key)]
        # And the purge genuinely proceeded (proving the drain doesn't
        # accidentally block deactivation itself).
        assert not os.path.exists(repo_dir)

    def test_drain_completes_before_the_phase1_rename_not_after(
        self, temp_data_dir, golden_repo_manager_mock, background_job_manager_mock
    ):
        """Codex HIGH finding (round 5): the drain wait must complete
        BEFORE the destructive Phase-1 rename-to-trash, not after. The
        prior ordering (rename first, drain second) meant a reader mid-
        query could have the directory yanked out from under it via the
        rename BEFORE the drain even started checking for stragglers --
        exactly the race the drain was supposed to prevent.

        Proven via a REAL QueryTracker subclass (a controlled test
        double extending the genuine collaborator, never mocking the
        SUT itself) that records, on its VERY FIRST get_ref_count()
        call -- the first thing wait_for_activated_repo_query_drain()
        does -- whether repo_dir still physically exists on disk at its
        ORIGINAL (pre-rename) location. If the rename ran first, the
        directory would already be gone (moved into .trash) by the time
        the drain's first refcount check fires.
        """
        manager = ActivatedRepoManager(
            data_dir=temp_data_dir,
            golden_repo_manager=golden_repo_manager_mock,
            background_job_manager=background_job_manager_mock,
        )

        username = "testuser"
        user_alias = "my-repo"
        user_dir = os.path.join(manager.activated_repos_dir, username)
        os.makedirs(user_dir, exist_ok=True)
        repo_dir = os.path.join(user_dir, user_alias)
        os.makedirs(repo_dir, exist_ok=True)
        with open(os.path.join(repo_dir, "marker.txt"), "w") as f:
            f.write("x")

        repo_data = {
            "user_alias": user_alias,
            "golden_repo_alias": "test-repo",
            "current_branch": "main",
            "activated_at": "2026-01-01T00:00:00+00:00",
            "last_accessed": "2026-01-01T00:00:00+00:00",
        }
        with open(os.path.join(user_dir, f"{user_alias}_metadata.json"), "w") as f:
            json.dump(repo_data, f)

        class _RecordingQueryTracker(QueryTracker):
            def __init__(self, watched_path: str) -> None:
                super().__init__()
                self._watched_path = watched_path
                self.get_ref_count_calls = 0
                self.watched_path_existed_on_first_check: Optional[bool] = None

            def get_ref_count(self, path: str) -> int:
                self.get_ref_count_calls += 1
                if self.get_ref_count_calls == 1:
                    self.watched_path_existed_on_first_check = os.path.exists(
                        self._watched_path
                    )
                return int(super().get_ref_count(path))

        recording_tracker = _RecordingQueryTracker(repo_dir)
        manager.set_query_tracker(recording_tracker)

        manager.deactivate_repository(username, user_alias)

        call_args = background_job_manager_mock.submit_job.call_args
        worker_fn = call_args[0][1]
        worker_fn(username=username, user_alias=user_alias)

        assert recording_tracker.get_ref_count_calls >= 1, (
            "Drain never checked the refcount at all -- cannot prove ordering."
        )
        assert recording_tracker.watched_path_existed_on_first_check is True, (
            "Bug: repo_dir no longer existed at its ORIGINAL location by "
            "the time the drain wait made its first refcount check -- the "
            "Phase-1 rename-to-trash ran BEFORE the drain wait, letting an "
            "in-flight reader have the directory yanked out from under it "
            "before the drain even started checking for stragglers."
        )
        # And the purge genuinely proceeded afterward (proving the drain
        # doesn't accidentally block deactivation itself).
        assert not os.path.exists(repo_dir)

    def test_composite_drain_completes_before_the_phase1_rename_not_after(
        self, temp_data_dir, golden_repo_manager_mock, background_job_manager_mock
    ):
        """Codex round-6 HIGH finding #6 (matches Claude's independent
        finding): _do_deactivate_composite renames at Phase 1 THEN
        drains -- the reverse of _do_deactivate_single's already-fixed
        ordering. Same real-QueryTracker-subclass proof technique: the
        drain's first get_ref_count() check must observe repo_path still
        existing at its ORIGINAL location.

        _stop_composite_services is NOT mocked -- it genuinely no-ops
        (returns early, no subprocess call) when repo_path has no
        .code-indexer directory, which this fixture deliberately omits,
        so the REAL method runs safely without touching the SUT."""
        manager = ActivatedRepoManager(
            data_dir=temp_data_dir,
            golden_repo_manager=golden_repo_manager_mock,
            background_job_manager=background_job_manager_mock,
        )

        username = "testuser"
        user_alias = "composite-repo"
        repo_path = os.path.join(manager.activated_repos_dir, username, user_alias)
        os.makedirs(repo_path, exist_ok=True)
        with open(os.path.join(repo_path, "marker.txt"), "w") as f:
            f.write("x")

        metadata = {
            "user_alias": user_alias,
            "path": repo_path,
            "is_composite": True,
            "username": username,
        }

        class _RecordingQueryTracker(QueryTracker):
            def __init__(self, watched_path: str) -> None:
                super().__init__()
                self._watched_path = watched_path
                self.get_ref_count_calls = 0
                self.watched_path_existed_on_first_check: Optional[bool] = None

            def get_ref_count(self, path: str) -> int:
                self.get_ref_count_calls += 1
                if self.get_ref_count_calls == 1:
                    self.watched_path_existed_on_first_check = os.path.exists(
                        self._watched_path
                    )
                return int(super().get_ref_count(path))

        recording_tracker = _RecordingQueryTracker(repo_path)
        manager.set_query_tracker(recording_tracker)

        manager._do_deactivate_composite(username, user_alias, metadata)

        assert recording_tracker.get_ref_count_calls >= 1, (
            "Drain never checked the refcount at all -- cannot prove ordering."
        )
        assert recording_tracker.watched_path_existed_on_first_check is True, (
            "Bug: repo_path no longer existed at its ORIGINAL location by "
            "the time the drain wait made its first refcount check -- the "
            "composite Phase-1 rename-to-trash ran BEFORE the drain wait."
        )
        assert not os.path.exists(repo_path)

    def test_single_deactivation_marks_quiescing_before_drain_and_clears_after(
        self, temp_data_dir, golden_repo_manager_mock, background_job_manager_mock
    ):
        """Codex round-6 HIGH finding #6b: the real admission barrier
        needs the QueryTracker.mark_quiescing() primitive to actually be
        CALLED by deactivation, before the drain wait, and cleared once
        deactivation reaches its terminal state (so a later reactivation
        at the same path is never wrongly refused). Proven via a real
        QueryTracker subclass (a controlled test double extending the
        genuine collaborator) that records is_quiescing() state at the
        drain's first real refcount check -- never mocking the SUT's own
        wait_for_activated_repo_query_drain function."""
        manager = ActivatedRepoManager(
            data_dir=temp_data_dir,
            golden_repo_manager=golden_repo_manager_mock,
            background_job_manager=background_job_manager_mock,
        )

        username = "testuser"
        user_alias = "my-repo"
        user_dir = os.path.join(manager.activated_repos_dir, username)
        os.makedirs(user_dir, exist_ok=True)
        repo_dir = os.path.join(user_dir, user_alias)
        os.makedirs(repo_dir, exist_ok=True)
        with open(os.path.join(repo_dir, "marker.txt"), "w") as f:
            f.write("x")

        repo_data = {
            "user_alias": user_alias,
            "golden_repo_alias": "test-repo",
            "current_branch": "main",
            "activated_at": "2026-01-01T00:00:00+00:00",
            "last_accessed": "2026-01-01T00:00:00+00:00",
        }
        with open(os.path.join(user_dir, f"{user_alias}_metadata.json"), "w") as f:
            json.dump(repo_data, f)

        class _RecordingQueryTracker(QueryTracker):
            def __init__(self, watched_path: str) -> None:
                super().__init__()
                self._watched_path = watched_path
                self.get_ref_count_calls = 0
                self.quiescing_at_first_check: Optional[bool] = None

            def get_ref_count(self, path: str) -> int:
                self.get_ref_count_calls += 1
                if self.get_ref_count_calls == 1:
                    self.quiescing_at_first_check = self.is_quiescing(
                        self._watched_path
                    )
                return int(super().get_ref_count(path))

        recording_tracker = _RecordingQueryTracker(repo_dir)
        manager.set_query_tracker(recording_tracker)

        manager.deactivate_repository(username, user_alias)

        call_args = background_job_manager_mock.submit_job.call_args
        worker_fn = call_args[0][1]
        worker_fn(username=username, user_alias=user_alias)

        assert recording_tracker.get_ref_count_calls >= 1, (
            "Drain never checked the refcount at all -- cannot prove "
            "the quiescing mark's timing."
        )
        assert recording_tracker.quiescing_at_first_check is True, (
            "Bug: the path was not marked quiescing BEFORE the drain wait "
            "ran -- the admission barrier primitive exists but is never "
            "actually invoked by deactivation."
        )
        assert recording_tracker.is_quiescing(repo_dir) is False, (
            "Bug: the quiescing mark was never cleared after deactivation "
            "completed -- a later reactivation at the same path would be "
            "permanently and incorrectly refused."
        )

    def test_composite_deactivation_marks_quiescing_before_drain_and_clears_after(
        self, temp_data_dir, golden_repo_manager_mock, background_job_manager_mock
    ):
        """Codex round-6 HIGH finding #6b explicitly covers BOTH
        drain-then-rename orderings -- composite deactivation must also
        mark/clear quiescing, not just the single-repo path."""
        manager = ActivatedRepoManager(
            data_dir=temp_data_dir,
            golden_repo_manager=golden_repo_manager_mock,
            background_job_manager=background_job_manager_mock,
        )

        username = "testuser"
        user_alias = "composite-repo"
        repo_path = os.path.join(manager.activated_repos_dir, username, user_alias)
        os.makedirs(repo_path, exist_ok=True)
        with open(os.path.join(repo_path, "marker.txt"), "w") as f:
            f.write("x")

        metadata = {
            "user_alias": user_alias,
            "path": repo_path,
            "is_composite": True,
            "username": username,
        }

        class _RecordingQueryTracker(QueryTracker):
            def __init__(self, watched_path: str) -> None:
                super().__init__()
                self._watched_path = watched_path
                self.get_ref_count_calls = 0
                self.quiescing_at_first_check: Optional[bool] = None

            def get_ref_count(self, path: str) -> int:
                self.get_ref_count_calls += 1
                if self.get_ref_count_calls == 1:
                    self.quiescing_at_first_check = self.is_quiescing(
                        self._watched_path
                    )
                return int(super().get_ref_count(path))

        recording_tracker = _RecordingQueryTracker(repo_path)
        manager.set_query_tracker(recording_tracker)

        manager._do_deactivate_composite(username, user_alias, metadata)

        assert recording_tracker.get_ref_count_calls >= 1
        assert recording_tracker.quiescing_at_first_check is True, (
            "Bug: the composite path was not marked quiescing BEFORE the "
            "drain wait ran."
        )
        assert recording_tracker.is_quiescing(repo_path) is False, (
            "Bug: the composite quiescing mark was never cleared after "
            "deactivation completed."
        )
