"""wait_for_activated_repo_query_drain() -- Story #1458 AC13 bounded
wait-then-proceed drain, reusing the SAME shape Story #1457 AC11
establishes: a config-sourced bound, and on expiry LOG a WARNING and
PROCEED with the purge -- never an unbounded/blocking wait that could
wedge deactivation.

Real QueryTracker, real (short, test-scoped) timing -- no mocking of the
tracker or the wait loop itself. The config-service READ is the only
monkeypatched surface, to keep this test bounded/fast regardless of the
real production default value.
"""

import logging
import threading
import time
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.server.services.deactivation_query_drain import (
    wait_for_activated_repo_query_drain,
)

# Codex MEDIUM finding (round 5) test constants -- named to avoid magic
# numbers in test_non_finite_max_wait_seconds_is_clamped_to_a_sane_bound.
TEST_CLAMP_SECONDS = 0.2
MAX_ALLOWED_ELAPSED_SECONDS = 2.0
MIN_EXPECTED_ELAPSED_SECONDS = 0.15


class TestWaitForActivatedRepoQueryDrain:
    def test_returns_immediately_when_refcount_already_zero(self):
        tracker = QueryTracker()
        start = time.monotonic()

        wait_for_activated_repo_query_drain(tracker, "/some/path", max_wait_seconds=5.0)

        assert time.monotonic() - start < 1.0

    def test_returns_once_refcount_drops_to_zero_before_deadline(self):
        tracker = QueryTracker()
        tracker.increment_ref("/some/path")

        def _release_shortly():
            time.sleep(0.2)
            tracker.decrement_ref("/some/path")

        threading.Thread(target=_release_shortly).start()

        start = time.monotonic()
        wait_for_activated_repo_query_drain(tracker, "/some/path", max_wait_seconds=5.0)
        elapsed = time.monotonic() - start

        assert 0.15 <= elapsed < 4.0
        assert tracker.get_ref_count("/some/path") == 0

    def test_expires_and_logs_warning_when_refcount_never_reaches_zero(self, caplog):
        tracker = QueryTracker()
        tracker.increment_ref("/stuck/path")

        with caplog.at_level(logging.WARNING):
            start = time.monotonic()
            wait_for_activated_repo_query_drain(
                tracker, "/stuck/path", max_wait_seconds=0.3
            )
            elapsed = time.monotonic() - start

        assert elapsed >= 0.3
        assert "/stuck/path" in caplog.text

    def test_none_query_tracker_is_a_noop(self):
        # Fail-open: no tracker configured (e.g. solo/CLI) must never block.
        start = time.monotonic()
        wait_for_activated_repo_query_drain(None, "/some/path", max_wait_seconds=5.0)
        assert time.monotonic() - start < 1.0

    def test_non_finite_max_wait_seconds_is_clamped_to_a_sane_bound(
        self, monkeypatch, caplog
    ):
        """Codex MEDIUM finding (round 5): drain duration had no finite
        upper-bound validation. A misconfigured (or NaN, from a bad
        config-read) max_wait_seconds of float('inf') would otherwise
        let this 'bounded wait' become genuinely unbounded, defeating
        the module's own core guarantee (per its docstring) that
        deactivation is 'never an unbounded/blocking wait that could
        wedge deactivation'. Clamps to a small test-scoped ceiling
        (monkeypatched module constant, not the SUT's logic, and
        raising=False since the constant does not exist yet -- this is
        the RED phase, the constant is added by the production fix)
        so the test itself stays fast while proving the REAL clamp
        logic runs."""
        import code_indexer.server.services.deactivation_query_drain as drain_module

        monkeypatch.setattr(
            drain_module,
            "_ABSOLUTE_MAX_DRAIN_WAIT_SECONDS",
            TEST_CLAMP_SECONDS,
            raising=False,
        )

        tracker = QueryTracker()
        tracker.increment_ref("/stuck/path")  # never released

        with caplog.at_level(logging.WARNING):
            start = time.monotonic()
            wait_for_activated_repo_query_drain(
                tracker, "/stuck/path", max_wait_seconds=float("inf")
            )
            elapsed = time.monotonic() - start

        assert elapsed < MAX_ALLOWED_ELAPSED_SECONDS, (
            "Bug: max_wait_seconds=inf was not clamped to a sane bound -- "
            f"the drain wait ran for {elapsed:.2f}s instead of returning "
            "near the clamped ceiling, meaning deactivation could be "
            "wedged indefinitely by a misconfigured or non-finite value."
        )
        assert elapsed >= MIN_EXPECTED_ELAPSED_SECONDS

    def test_reads_max_wait_from_config_when_not_explicitly_supplied(self):
        # The config-sourced bound is deliberately TINY (0.05s) while the
        # refcount is released much later (0.3s) -- if the config value
        # were ignored (e.g. some larger hardcoded/default timeout used
        # instead), this call would take ~0.3s and return with refcount=0.
        # Observing a fast return with the refcount STILL held proves the
        # tiny config-driven bound genuinely governed the wait.
        tracker = QueryTracker()
        tracker.increment_ref("/config-driven/path")

        def _release_much_later():
            time.sleep(0.3)
            tracker.decrement_ref("/config-driven/path")

        threading.Thread(target=_release_much_later).start()

        mock_config = MagicMock()
        mock_config.deactivation_query_drain_max_wait_seconds = 0.05

        with patch(
            "code_indexer.server.services.deactivation_query_drain.get_config_service"
        ) as mock_get_cfg:
            mock_get_cfg.return_value.get_config.return_value = mock_config

            start = time.monotonic()
            wait_for_activated_repo_query_drain(tracker, "/config-driven/path")
            elapsed = time.monotonic() - start

        mock_get_cfg.return_value.get_config.assert_called_once()
        assert elapsed < 0.25


class TestTrackActivatedRepoQuery:
    """Codex Finding #7 (HIGH): the shared refcount-tracking helper the
    REST (inline_query.py) and wiki (wiki/routes.py) query entry points
    need -- previously only MCP search.py wired this, so deactivation
    could observe zero in-flight queries and purge chunks.db mid-query via
    those other front doors."""

    def test_increments_and_decrements_using_the_drain_key_format(self) -> None:
        from code_indexer.server.services.deactivation_query_drain import (
            track_activated_repo_query,
        )

        tracker = QueryTracker()
        arm = MagicMock()
        arm.activated_repos_dir = "/data/activated-repos"
        expected_key = "/data/activated-repos/alice/myrepo"

        with track_activated_repo_query(tracker, arm, "alice", "myrepo"):
            assert tracker.get_ref_count(expected_key) == 1

        assert tracker.get_ref_count(expected_key) == 0

    def test_decrements_even_when_the_wrapped_query_raises(self) -> None:
        from code_indexer.server.services.deactivation_query_drain import (
            track_activated_repo_query,
        )

        tracker = QueryTracker()
        arm = MagicMock()
        arm.activated_repos_dir = "/data/activated-repos"
        expected_key = "/data/activated-repos/bob/theirrepo"

        try:
            with track_activated_repo_query(tracker, arm, "bob", "theirrepo"):
                assert tracker.get_ref_count(expected_key) == 1
                raise ValueError("simulated query failure")
        except ValueError:
            pass

        assert tracker.get_ref_count(expected_key) == 0

    def test_noop_when_query_tracker_is_none(self) -> None:
        from code_indexer.server.services.deactivation_query_drain import (
            track_activated_repo_query,
        )

        arm = MagicMock()
        arm.activated_repos_dir = "/data/activated-repos"

        with track_activated_repo_query(None, arm, "alice", "myrepo"):
            pass  # must not raise


class TestTrackActivatedRepoQueryQuiescingAdmissionBarrier:
    """Codex round-6 HIGH finding #6b: drain-then-rename reordering alone
    left a residual late-admission race -- drain observes zero refcount,
    a NEW query then resolves the still-active metadata and increments
    its OWN refcount, rename proceeds anyway (drain already returned),
    and the query then reads a path that's already gone. Fix: query
    admission must refuse to even START (never increment_ref, never let
    the wrapped query run) once QueryTracker.mark_quiescing() has been
    called for that path."""

    def test_refuses_admission_when_path_is_marked_quiescing(self) -> None:
        from code_indexer.server.services.deactivation_query_drain import (
            RepositoryDeactivatingError,
            track_activated_repo_query,
        )

        tracker = QueryTracker()
        arm = MagicMock()
        arm.activated_repos_dir = "/data/activated-repos"
        expected_key = "/data/activated-repos/alice/myrepo"

        tracker.mark_quiescing(expected_key)

        query_ran = {"value": False}
        with pytest.raises(RepositoryDeactivatingError):
            with track_activated_repo_query(tracker, arm, "alice", "myrepo"):
                query_ran["value"] = True

        assert query_ran["value"] is False, (
            "Bug: the wrapped query body was allowed to run even though "
            "the path was marked quiescing -- admission must refuse "
            "BEFORE the query executes, not merely fail to track it."
        )
        # No leaked refcount from a refused admission attempt.
        assert tracker.get_ref_count(expected_key) == 0

    def test_admission_succeeds_normally_when_not_quiescing(self) -> None:
        from code_indexer.server.services.deactivation_query_drain import (
            track_activated_repo_query,
        )

        tracker = QueryTracker()
        arm = MagicMock()
        arm.activated_repos_dir = "/data/activated-repos"
        expected_key = "/data/activated-repos/alice/myrepo"

        with track_activated_repo_query(tracker, arm, "alice", "myrepo"):
            assert tracker.get_ref_count(expected_key) == 1

        assert tracker.get_ref_count(expected_key) == 0

    def test_admission_succeeds_when_query_tracker_is_a_bare_unconfigured_magicmock(
        self,
    ) -> None:
        """Regression confirmed by server-fast-automation.sh: many existing
        tests (e.g. test_search_event_instrumentation.py) pass a bare,
        never-explicitly-configured MagicMock() as query_tracker (via
        app.state.query_tracker defaulting from an unconfigured parent
        MagicMock). unittest.mock.MagicMock() instances are TRUTHY by
        default for any unconfigured method call -- so
        `if query_tracker.is_quiescing(refcount_key):` incorrectly
        treats every such mock as 'quiescing' and refuses admission
        unconditionally, breaking every test using this widespread,
        pre-existing test-double pattern. The admission check must only
        refuse on a genuine `True` result, never on truthy-Mock noise."""
        from code_indexer.server.services.deactivation_query_drain import (
            track_activated_repo_query,
        )

        tracker = MagicMock()  # is_quiescing() unconfigured -> truthy Mock
        arm = MagicMock()
        arm.activated_repos_dir = "/data/activated-repos"

        query_ran = {"value": False}
        with track_activated_repo_query(tracker, arm, "alice", "myrepo"):
            query_ran["value"] = True

        assert query_ran["value"] is True, (
            "Bug: a bare, unconfigured MagicMock() query_tracker caused "
            "admission to be refused -- MagicMock's default truthy "
            "unconfigured-method-call behavior must never be mistaken "
            "for a genuine is_quiescing()==True result."
        )


class TestTrackActivatedRepoQueryAtomicAdmissionRace:
    """Round-8 HIGH finding (Codex empirical reproduction): the round-6
    admission barrier used to check `is_quiescing()` and then, separately,
    call `increment_ref()` -- two INDEPENDENT critical sections, each
    acquiring QueryTracker's internal lock on its own. A worker thread's
    `is_quiescing()` call could return False, get paused right after,
    while another thread called `mark_quiescing()` on the SAME key in
    that gap -- and the paused worker's `increment_ref()` still
    succeeded once resumed. Result: a query got tracked as active AFTER
    quiescing had already begun.

    The GREEN-phase fix (`QueryTracker.try_increment_ref_if_not_
    quiescing`, called by `track_activated_repo_query` instead of the
    old two-step sequence) closes this by performing the check and the
    increment inside ONE lock acquisition -- which also means the
    original reproduction technique (pausing a worker thread INSIDE
    `is_quiescing()`, between the check and the increment) no longer has
    a seam to attach to: production code never calls `is_quiescing()`
    standalone anymore. That the old pausing hook can no longer even be
    reached is itself evidence the race window is gone. This test
    instead directly proves the atomicity guarantee the fix relies on:
    `try_increment_ref_if_not_quiescing()` and `mark_quiescing()` share
    the SAME internal lock, so a call to one cannot interleave with the
    other -- proven by holding the lock from the main thread and showing
    a concurrent `try_increment_ref_if_not_quiescing()` call blocks
    until the lock is released (a bounded `Thread.join` + `is_alive()`
    check, never a raw sleep-based race)."""

    def test_admission_and_mark_quiescing_share_the_same_lock_no_interleaving_possible(
        self,
    ) -> None:
        tracker = QueryTracker()
        path = "/data/activated-repos/alice/myrepo"

        result: dict = {}

        def worker() -> None:
            result["admitted"] = tracker.try_increment_ref_if_not_quiescing(path)

        tracker._lock.acquire()
        try:
            worker_thread = threading.Thread(target=worker)
            worker_thread.start()

            # Bounded wait to prove the worker is genuinely BLOCKED on
            # the shared lock (never completed) while the main thread
            # still holds it -- not a raw sleep-based timing guess.
            worker_thread.join(timeout=0.3)
            assert worker_thread.is_alive(), (
                "Bug: try_increment_ref_if_not_quiescing() completed "
                "while the tracker's internal lock was still held by "
                "another thread -- it is not using the shared lock for "
                "atomicity, so it could interleave with a concurrent "
                "mark_quiescing() call exactly like the round-8 race."
            )
            assert "admitted" not in result
        finally:
            tracker._lock.release()

        worker_thread.join(timeout=5.0)
        assert result["admitted"] is True, (
            "Once the lock was released, the worker should have been "
            "admitted (the path was never marked quiescing)."
        )
        assert tracker.get_ref_count(path) == 1


class TestTrackActivatedRepoQueryOutOfScopeCases:
    """Cases where the helper must be a true no-op (never construct a key
    or touch the tracker at all)."""

    def test_noop_for_global_alias_out_of_scope_for_this_helper(self) -> None:
        from code_indexer.server.services.deactivation_query_drain import (
            track_activated_repo_query,
        )

        tracker = QueryTracker()
        arm = MagicMock()
        arm.activated_repos_dir = "/data/activated-repos"

        with track_activated_repo_query(tracker, arm, "alice", "myrepo-global"):
            pass

        assert tracker.get_all_paths() == set()

    def test_noop_when_repository_alias_is_none(self) -> None:
        from code_indexer.server.services.deactivation_query_drain import (
            track_activated_repo_query,
        )

        tracker = QueryTracker()
        arm = MagicMock()
        arm.activated_repos_dir = "/data/activated-repos"

        with track_activated_repo_query(tracker, arm, "alice", None):
            pass

        assert tracker.get_all_paths() == set()

    def test_noop_when_activated_repo_manager_is_none(self) -> None:
        from code_indexer.server.services.deactivation_query_drain import (
            track_activated_repo_query,
        )

        tracker = QueryTracker()

        with track_activated_repo_query(tracker, None, "alice", "myrepo"):
            pass  # must not raise (no activated_repos_dir to read)

        assert tracker.get_all_paths() == set()

        # Let the background release thread finish before the test ends
        # (harmless bookkeeping -- the tracker instance is not reused).
        time.sleep(0.35)
