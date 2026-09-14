"""Regression tests for Bug #1825.

`TemporalWatchHandler._start_polling_thread()` used to run `while True:
time.sleep(5); ... subprocess.run(["git", "rev-parse", "HEAD"], ...)` with NO
way to ever stop it -- no event, no join, no `stop()` method anywhere in the
class. Any test (or real `cidx watch` session) that triggered the polling
fallback leaked a real daemon background thread for the rest of the
process's life, firing a real `git rev-parse HEAD` subprocess call every 5
seconds. In the test suite this polluted an UNRELATED test's exact
subprocess-call-count assertions
(tests/unit/services/test_reconcile_batch_content_id_1505.py) whenever one of
those periodic ticks happened to land inside that test's narrow counting
window -- the actual root cause of Bug #1825's intermittent +1/+2 failures.

The leak was traced (via direct source review, confirmed with live stack-
trace instrumentation) to FOUR tests in
tests/unit/cli/test_temporal_watch_handler.py::TestTemporalWatchHandlerInit
that each construct a real (non-Thread-mocked) TemporalWatchHandler against a
project_root with no matching `.git/refs/heads/<branch>` file, so
`use_polling` becomes True and a real thread starts:
  - test_init_without_git_refs_file_uses_polling (no .git dir at all)
  - test_get_current_branch_success (no .git dir at all)
  - test_get_current_branch_detached_head (no .git dir at all)
  - test_get_last_commit_hash_success (no .git dir at all)

Those tests are fixed at the leak (either by giving them a matching refs
file so the polling fallback is never entered, since none of them are
actually testing polling behavior, or -- for the one test that IS testing
polling behavior -- by calling the new `stop()` method). This file guards
the production capability those fixes now depend on.
"""

import threading
import time
from unittest.mock import Mock, patch

from code_indexer.cli_temporal_watch_handler import (
    TemporalWatchHandler,
    _POLLING_INTERVAL_SECONDS,
)

# Bounded wait mirrors ActivityHeartbeatWriter.stop()'s own contract (one
# full poll interval plus grace) -- generous, never indefinite.
_THREAD_JOIN_GRACE_SECONDS = 2
_THREAD_JOIN_TIMEOUT_SECONDS = _POLLING_INTERVAL_SECONDS + _THREAD_JOIN_GRACE_SECONDS
# Short settle delay after a successful join, before re-enumerating threads,
# to let the interpreter finish tearing down the finished thread object.
_THREAD_EXIT_SETTLE_SECONDS = 0.05


def _make_polling_handler(tmp_path, branch: str = "main", commit: str = "abc123def"):
    """Construct a real TemporalWatchHandler that falls back to polling
    (project_root has no .git directory at all, so the refs file can never
    be found), with only subprocess.run mocked -- threading.Thread is left
    real so this proves the actual production thread lifecycle."""
    project_root = tmp_path / "repo"
    project_root.mkdir()
    with patch("code_indexer.cli_temporal_watch_handler.subprocess.run") as mock_run:
        mock_run.side_effect = [
            Mock(stdout=f"{branch}\n", returncode=0),
            Mock(stdout=f"{commit}\n", returncode=0),
        ]
        handler = TemporalWatchHandler(project_root)
    return handler


def _assert_thread_terminates(thread: threading.Thread) -> None:
    """Bounded join + assert the thread is actually gone, never alive."""
    thread.join(timeout=_THREAD_JOIN_TIMEOUT_SECONDS)
    assert not thread.is_alive(), (
        "Bug #1825 regression: the polling thread did not terminate within "
        f"the expected {_THREAD_JOIN_TIMEOUT_SECONDS}s join timeout after "
        "stop() was called."
    )


class TestPollingThreadLifecycle:
    def test_polling_fallback_starts_a_real_alive_thread(self, tmp_path):
        """Sanity check: the polling fallback path genuinely starts a real,
        alive background thread (not a mock) -- establishes the precondition
        the rest of this file's tests depend on."""
        handler = _make_polling_handler(tmp_path)
        try:
            assert handler.use_polling is True
            assert handler._polling_thread is not None
            assert handler._polling_thread.is_alive()
        finally:
            handler.stop()

    def test_stop_terminates_the_real_polling_thread(self, tmp_path):
        """Bug #1825's core regression guard: stop() must actually join the
        background thread, not merely signal it and return."""
        handler = _make_polling_handler(tmp_path)

        handler.stop()

        _assert_thread_terminates(handler._polling_thread)


class TestStopSafetyAndIdempotency:
    def test_stop_is_safe_and_idempotent(self, tmp_path):
        """Calling stop() twice must never raise -- production shutdown
        paths (cli.py, cli_watch_helpers.py) may call it defensively (e.g.
        once from a finally block and once more from a nested cleanup
        path), so a second call must be a safe no-op."""
        handler = _make_polling_handler(tmp_path)
        handler.stop()
        handler.stop()  # must not raise

    def test_stop_is_a_safe_noop_when_polling_was_never_started(self, tmp_path):
        """A handler that used real inotify monitoring (use_polling False)
        never started a thread; stop() must be a safe no-op."""
        project_root = tmp_path / "repo"
        project_root.mkdir()
        git_dir = project_root / ".git"
        git_dir.mkdir()
        refs_heads = git_dir / "refs" / "heads"
        refs_heads.mkdir(parents=True)
        (refs_heads / "main").touch()

        with patch(
            "code_indexer.cli_temporal_watch_handler.subprocess.run"
        ) as mock_run:
            mock_run.side_effect = [
                Mock(stdout="main\n", returncode=0),
                Mock(stdout="abc123\n", returncode=0),
            ]
            handler = TemporalWatchHandler(project_root)

        assert handler.use_polling is False
        assert handler._polling_thread is None
        handler.stop()  # must not raise


class TestNoThreadSurvivesFullCycle:
    def test_no_thread_survives_a_full_construct_and_stop_cycle(self, tmp_path):
        """Process-wide discrimination proof: after stop(), the thread
        threading.enumerate() would have reported is gone -- proving this is
        not merely a state-flag flip but a real, joined thread."""
        pre_existing = {id(t) for t in threading.enumerate()}

        handler = _make_polling_handler(tmp_path)
        handler.stop()
        _assert_thread_terminates(handler._polling_thread)
        time.sleep(_THREAD_EXIT_SETTLE_SECONDS)

        leaked = [
            t
            for t in threading.enumerate()
            if id(t) not in pre_existing and t.is_alive()
        ]
        assert leaked == [], (
            f"Bug #1825 regression: {len(leaked)} thread(s) leaked past "
            f"stop(): {[t.name for t in leaked]}"
        )
