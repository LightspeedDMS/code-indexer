"""Bug #1800: prove the leaked-non-daemon-thread guard actually discriminates.

The guard in ``tests/unit/conftest.py`` is what turns a silent
server-fast-automation hang into a loud failure. A guard that never fires is
worse than none at all, so these tests drive it with REAL threads: one that
must be reported, and two that must not.
"""

import threading

import pytest

from tests.unit.conftest import _surviving_non_daemon_threads

# Bounded join (Messi Rule #14): a test about leaked threads must never leak one.
_JOIN_TIMEOUT_SECONDS = 10.0


@pytest.fixture
def parked_threads():
    """Yield a factory for real threads parked until the test releases them."""
    release = threading.Event()
    started: list = []

    def _start(*, daemon: bool) -> threading.Thread:
        ready = threading.Event()

        def _park() -> None:
            ready.set()
            release.wait(_JOIN_TIMEOUT_SECONDS)

        thread = threading.Thread(target=_park, daemon=daemon)
        thread.start()
        assert ready.wait(_JOIN_TIMEOUT_SECONDS), "thread never started"
        started.append(thread)
        return thread

    yield _start

    release.set()
    for thread in started:
        thread.join(timeout=_JOIN_TIMEOUT_SECONDS)
        assert not thread.is_alive(), f"{thread.name} outlived its own test"


class TestGuardDetectsTheLeak:
    """The RED half: the condition that wedges the gate must be reported."""

    def test_non_daemon_thread_started_after_baseline_is_reported(self, parked_threads):
        baseline_ids = {id(t) for t in threading.enumerate()}

        leaked = parked_threads(daemon=False)

        reported = _surviving_non_daemon_threads(baseline_ids)
        assert leaked in reported, (
            "the guard did not report a live non-daemon thread started during "
            "the session -- it would not have caught Bug #1800"
        )


class TestGuardDoesNotFireVacuously:
    """The GREEN half: threads that cannot block interpreter exit are ignored."""

    def test_daemon_thread_is_not_reported(self, parked_threads):
        baseline_ids = {id(t) for t in threading.enumerate()}

        daemonic = parked_threads(daemon=True)

        reported = _surviving_non_daemon_threads(baseline_ids)
        assert daemonic not in reported, (
            "daemon threads do not block interpreter exit and must not fail the guard"
        )

    def test_thread_already_running_at_baseline_is_not_reported(self, parked_threads):
        pre_existing = parked_threads(daemon=False)
        baseline_ids = {id(t) for t in threading.enumerate()}

        reported = _surviving_non_daemon_threads(baseline_ids)
        assert pre_existing not in reported, (
            "a thread already running before the session is not this session's "
            "leak to report"
        )
