"""Bug #1549: primary-instance startup guard.

acquire_primary_instance_lock() lets a server process prove, before
running any destructive startup orphan-cleanup sweep, that no other
process is currently alive holding the same lock for a given server data
directory. Backed by a real OS file lock (flock/fcntl via `filelock`),
which the kernel releases automatically on process exit -- including a
hard kill -- so a genuinely-dead previous instance never blocks a real
restart from acquiring it.
"""

import threading
import time

import filelock

from code_indexer.server.utils.primary_instance_lock import (
    acquire_primary_instance_lock,
    release_primary_instance_lock,
)

# Bug #1549 Finding 1 test constants: kept small so the test suite stays
# fast, with enough separation between the release delay and the acquire
# bound to make the assertions unambiguous.
_PREDECESSOR_RELEASE_DELAY_SECONDS = 0.3
_SUCCESSFUL_ACQUIRE_BOUND_SECONDS = 2.0
_EVENT_WAIT_SAFETY_SECONDS = 5.0
_REFUSAL_ACQUIRE_BOUND_SECONDS = 0.3


class TestAcquirePrimaryInstanceLockBoundedBlocking:
    """Bug #1549 Finding 1 (Codex-confirmed): acquire_primary_instance_lock
    was strictly non-blocking (timeout=0). On a REAL restart where the
    outgoing process has not fully exited yet (slow lifespan teardown,
    systemd Restart=always overlap), the incoming legitimate process fails
    to acquire and skips the sweeps FOREVER (the sweep only ever runs at
    startup). Fix: a bounded blocking acquire -- a genuinely-dead
    predecessor releases its kernel lock within milliseconds of exiting,
    so a bounded wait covers restart overlap, while a genuinely-alive
    duplicate holding the lock for the whole wait is still refused."""

    def test_acquire_blocks_and_succeeds_once_a_slow_predecessor_releases_within_bound(
        self, tmp_path
    ) -> None:
        lock_path = str(tmp_path / "primary_instance.lock")
        # thread_local=False: the releaser thread below must be able to
        # release the SAME lock state the main thread acquired -- filelock
        # defaults to thread-local lock tracking, which would make a
        # cross-thread release a no-op.
        predecessor = filelock.FileLock(lock_path, timeout=0, thread_local=False)
        predecessor.acquire(timeout=0)
        assert predecessor.is_locked, "test setup failed: predecessor did not acquire"

        # Deterministic ordering (no timing-based race): the releaser
        # thread blocks on this event and only starts its release delay
        # AFTER the main thread has set it, which happens immediately
        # before calling acquire_primary_instance_lock -- so the
        # predecessor is guaranteed to still be held for at least
        # _PREDECESSOR_RELEASE_DELAY_SECONDS after the acquire attempt
        # begins, regardless of thread-scheduling variance.
        acquire_attempt_starting = threading.Event()
        event_wait_outcome: list = []

        def release_after_delay() -> None:
            signaled = acquire_attempt_starting.wait(timeout=_EVENT_WAIT_SAFETY_SECONDS)
            event_wait_outcome.append(signaled)
            if not signaled:
                # Never observed the main thread's signal -- do not release,
                # so the failure surfaces clearly via the outcome assertion
                # below rather than silently proceeding on a bad premise.
                return
            time.sleep(_PREDECESSOR_RELEASE_DELAY_SECONDS)
            predecessor.release()

        releaser = threading.Thread(target=release_after_delay)
        releaser.start()
        try:
            start = time.monotonic()
            acquire_attempt_starting.set()
            acquired = acquire_primary_instance_lock(
                str(tmp_path), timeout=_SUCCESSFUL_ACQUIRE_BOUND_SECONDS
            )
            elapsed = time.monotonic() - start
            releaser.join(timeout=_EVENT_WAIT_SAFETY_SECONDS)
            assert event_wait_outcome == [True], (
                "test setup failed: releaser thread never observed the "
                "start-of-acquire signal"
            )
            assert acquired is True, (
                "a restarting process must become primary once the "
                "predecessor's lock is released within the bound"
            )
            assert elapsed >= _PREDECESSOR_RELEASE_DELAY_SECONDS, (
                "acquisition returned before the predecessor could have "
                "released -- expected it to wait rather than fail-fast "
                f"(non-blocking timeout=0 bug), elapsed={elapsed}"
            )
            assert elapsed < _SUCCESSFUL_ACQUIRE_BOUND_SECONDS, (
                f"acquisition took longer than the configured bound, elapsed={elapsed}"
            )
        finally:
            releaser.join()
            release_primary_instance_lock(str(tmp_path))

    def test_still_refused_when_a_live_instance_holds_the_lock_beyond_the_bound(
        self, tmp_path
    ) -> None:
        """A genuinely-alive duplicate/crash-looping process holding the
        lock for the WHOLE wait must still be correctly refused -- the
        bound must not become an unbounded wait that lets a duplicate
        eventually win."""
        lock_path = str(tmp_path / "primary_instance.lock")
        live_instance = filelock.FileLock(lock_path, timeout=0)
        live_instance.acquire(timeout=0)
        assert live_instance.is_locked, (
            "test setup failed: live_instance did not acquire"
        )
        try:
            start = time.monotonic()
            acquired = acquire_primary_instance_lock(
                str(tmp_path), timeout=_REFUSAL_ACQUIRE_BOUND_SECONDS
            )
            elapsed = time.monotonic() - start
            assert acquired is False
            assert elapsed >= _REFUSAL_ACQUIRE_BOUND_SECONDS, (
                f"refusal returned too fast for a bounded wait, elapsed={elapsed}"
            )
        finally:
            live_instance.release()


class TestAcquirePrimaryInstanceLock:
    def test_first_acquisition_succeeds(self, tmp_path) -> None:
        try:
            assert acquire_primary_instance_lock(str(tmp_path)) is True
        finally:
            release_primary_instance_lock(str(tmp_path))

    def test_second_concurrent_acquisition_against_same_dir_fails(
        self, tmp_path
    ) -> None:
        try:
            assert acquire_primary_instance_lock(str(tmp_path)) is True

            # Simulate a second, independent process attempting the same
            # lock while the first is still held -- non-blocking, False.
            competing_lock = filelock.FileLock(
                str(tmp_path / "primary_instance.lock"), timeout=0
            )
            try:
                competing_lock.acquire(timeout=0)
                acquired = True
                competing_lock.release()
            except filelock.Timeout:
                acquired = False
            assert acquired is False
        finally:
            release_primary_instance_lock(str(tmp_path))

    def test_reacquisition_succeeds_after_release(self, tmp_path) -> None:
        """Matches real behavior: a genuinely-dead prior instance releases
        the OS lock on exit, so a real restart can always acquire it."""
        assert acquire_primary_instance_lock(str(tmp_path)) is True
        release_primary_instance_lock(str(tmp_path))

        try:
            assert acquire_primary_instance_lock(str(tmp_path)) is True
        finally:
            release_primary_instance_lock(str(tmp_path))

    def test_independent_directories_do_not_conflict(self, tmp_path) -> None:
        dir_a = tmp_path / "a"
        dir_b = tmp_path / "b"
        dir_a.mkdir()
        dir_b.mkdir()

        try:
            assert acquire_primary_instance_lock(str(dir_a)) is True
            assert acquire_primary_instance_lock(str(dir_b)) is True
        finally:
            release_primary_instance_lock(str(dir_a))
            release_primary_instance_lock(str(dir_b))
