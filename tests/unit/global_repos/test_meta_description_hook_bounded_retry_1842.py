"""
Unit tests for Bug #1842 AC3 (optional mitigation): bounded retry on
transient cidx-meta write-lock micro-contention in
atomic_write_description() / on_repo_removed().

The dominant AC3 fix is diagnostics (AC4, covered elsewhere) plus NOT
narrowing the lock's hold window across the long-running lifecycle
preflight / Pass1/Pass2 Claude CLI calls (see the bug's final report for
why that narrowing was rejected as unsafe -- it would reopen the
RefreshScheduler cidx-meta race Bug #1506 fixed).

This bounded retry addresses a DIFFERENT, narrower case: two callers
racing to acquire the SAME lock within a few hundred milliseconds of each
other (e.g. two on_repo_added() calls in rapid succession). A single
non-blocking attempt fails outright even though the real holder releases
within milliseconds; a short, hard-bounded retry (a handful of attempts,
well under 1s total) self-heals that case while a genuinely long hold
(the actual bug being fixed) still correctly fails after the bound is
exhausted -- never an unbounded wait (Messi Rule #14).
"""

import threading
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from code_indexer.global_repos.lifecycle_batch_runner import (
    LifecycleLockUnavailableError,
)
from code_indexer.global_repos.meta_description_hook import (
    atomic_write_description,
    on_repo_removed,
    set_refresh_scheduler,
)
from code_indexer.global_repos.write_lock_manager import WriteLockManager

# Bounded wait for the background holder thread's handshake, well within
# the retry loop's own total budget.
_THREAD_JOIN_TIMEOUT_SECONDS = 5


@pytest.fixture(autouse=True)
def reset_module_state():
    import code_indexer.global_repos.meta_description_hook as hook_module

    original = getattr(hook_module, "_refresh_scheduler", None)
    hook_module._refresh_scheduler = None
    yield
    hook_module._refresh_scheduler = original


@pytest.fixture
def existing_repo_md_file(tmp_path):
    """Create golden_repos_dir/cidx-meta/some-repo.md and return its Path."""
    cidx_meta_path = Path(tmp_path) / "cidx-meta"
    cidx_meta_path.mkdir(parents=True)
    md_file = cidx_meta_path / "some-repo.md"
    md_file.write_text("# some-repo\n")
    return md_file


class TestAtomicWriteDescriptionBoundedRetry:
    def test_succeeds_after_a_transient_failure_then_success(self, tmp_path):
        """A caller whose first acquire attempt fails but a SUBSEQUENT
        attempt (within the bounded retry window) succeeds must write the
        file normally -- proving the retry loop actually retries."""
        scheduler = MagicMock()
        scheduler.acquire_write_lock.side_effect = [False, True]

        target = tmp_path / "transient.md"
        atomic_write_description(target, "content", refresh_scheduler=scheduler)

        assert target.read_text() == "content"
        assert scheduler.acquire_write_lock.call_count == 2
        scheduler.release_write_lock.assert_called_once()

    def test_gives_up_after_a_bounded_number_of_attempts(self, tmp_path):
        """A caller whose acquire attempt ALWAYS fails must still raise
        LifecycleLockUnavailableError -- but only after a SMALL, FIXED
        number of attempts, never an unbounded retry (Messi Rule #14)."""
        scheduler = MagicMock()
        scheduler.acquire_write_lock.return_value = False

        target = tmp_path / "never_acquired.md"
        with pytest.raises(LifecycleLockUnavailableError):
            atomic_write_description(target, "content", refresh_scheduler=scheduler)

        assert not target.exists()
        # A hard, small ceiling -- this pins the exact bound so a future
        # change to it is a deliberate, reviewed decision, not a silent
        # drift toward an unbounded loop.
        assert 1 < scheduler.acquire_write_lock.call_count <= 5, (
            scheduler.acquire_write_lock.call_count
        )
        scheduler.release_write_lock.assert_not_called()

    def test_real_lock_released_by_another_thread_mid_retry_self_heals(self, tmp_path):
        """Real-concurrency discriminating case: a genuine WriteLockManager
        (Messi Rule #1 anti-mock) holds the 'cidx-meta' lock under a
        DIFFERENT owner on a background thread; that thread releases it
        only after the caller's first attempt has actually observed
        failure (a deterministic threading.Event handshake, Bug #1842
        Finding 4 -- not a racing sleep against the retry loop's own
        timing). The bounded retry must then observe the release and
        succeed -- proving this works against the real file-based locking
        primitive, not just a mocked side_effect sequence."""
        lock_manager = WriteLockManager(tmp_path)
        acquired = lock_manager.acquire("cidx-meta", owner_name="other_holder")
        assert acquired is True

        first_attempt_failed = threading.Event()

        def _release_after_first_failure():
            observed = first_attempt_failed.wait(timeout=_THREAD_JOIN_TIMEOUT_SECONDS)
            if observed:
                lock_manager.release("cidx-meta", owner_name="other_holder")
            # else: the test body will fail on its own assertions below;
            # nothing to release deterministically.

        releaser = threading.Thread(target=_release_after_first_failure)
        releaser.start()
        try:

            def _acquire_side_effect(alias, owner_name):
                result = lock_manager.acquire(alias, owner_name=owner_name)
                if not result:
                    first_attempt_failed.set()
                return result

            scheduler = MagicMock()
            scheduler.acquire_write_lock.side_effect = _acquire_side_effect
            scheduler.release_write_lock.side_effect = (
                lambda alias, owner_name: lock_manager.release(
                    alias, owner_name=owner_name
                )
            )

            target = tmp_path / "real_contention.md"
            atomic_write_description(target, "content", refresh_scheduler=scheduler)

            assert target.read_text() == "content"
            assert scheduler.acquire_write_lock.call_count > 1, (
                "expected at least one failed attempt before the real "
                "holder released the lock"
            )
        finally:
            # Unblock the releaser even if the test body raised before
            # the acquire side effect ever observed a failure.
            first_attempt_failed.set()
            releaser.join(timeout=_THREAD_JOIN_TIMEOUT_SECONDS)


class TestOnRepoRemovedBoundedRetry:
    def test_succeeds_after_a_transient_failure_then_success(
        self, tmp_path, existing_repo_md_file
    ):
        scheduler = MagicMock()
        scheduler.acquire_write_lock.side_effect = [False, True]
        scheduler.trigger_refresh_for_repo.return_value = "job-1"
        set_refresh_scheduler(scheduler)

        on_repo_removed(repo_name="some-repo", golden_repos_dir=str(tmp_path))

        assert not existing_repo_md_file.exists()
        assert scheduler.acquire_write_lock.call_count == 2
        scheduler.release_write_lock.assert_called_once()

    def test_gives_up_after_a_bounded_number_of_attempts(
        self, tmp_path, existing_repo_md_file
    ):
        scheduler = MagicMock()
        scheduler.acquire_write_lock.return_value = False
        set_refresh_scheduler(scheduler)

        on_repo_removed(repo_name="some-repo", golden_repos_dir=str(tmp_path))

        assert existing_repo_md_file.exists(), (
            "file must survive when lock is never acquired"
        )
        assert 1 < scheduler.acquire_write_lock.call_count <= 5, (
            scheduler.acquire_write_lock.call_count
        )
        scheduler.release_write_lock.assert_not_called()
