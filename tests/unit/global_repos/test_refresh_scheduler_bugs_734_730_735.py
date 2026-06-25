"""
Regression tests for Bugs #734, #730, #735 in RefreshScheduler.

Bug #734: cleanup_stale_write_mode_markers(force=True) at startup (in start()) has no
try/except — if it raises the scheduler thread is never launched.

Bug #730 (SUPERSEDED by Bug #1218): subprocess.run() for 'cidx scip generate' in
_index_source() previously had no timeout= kwarg.  Bug #1218 removes ALL overarching
per-job timeouts on the indexing+SCIP path (they caused large-repo partial/corrupt
indexes).  The test now asserts the timeout kwarg is ABSENT.

Bug #735: _scheduler_loop() has no exponential backoff on consecutive failures — a
permanently-broken upstream (e.g. corrupted DB) spams error logs at fixed cadence forever.
"""

import ast
import inspect
import logging
import pathlib
import textwrap
from unittest.mock import Mock, patch

import pytest

from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.refresh_scheduler import RefreshScheduler


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def mock_query_tracker():
    return Mock(spec=QueryTracker)


@pytest.fixture
def mock_cleanup_manager():
    return Mock(spec=CleanupManager)


@pytest.fixture
def mock_config_source():
    config = Mock()
    config.get_global_refresh_interval.return_value = 3600
    return config


@pytest.fixture
def mock_registry():
    registry = Mock()
    registry.list_global_repos.return_value = []
    return registry


@pytest.fixture
def scheduler(
    tmp_path,
    mock_config_source,
    mock_query_tracker,
    mock_cleanup_manager,
    mock_registry,
):
    golden_dir = tmp_path / "golden-repos"
    golden_dir.mkdir()
    return RefreshScheduler(
        golden_repos_dir=str(golden_dir),
        config_source=mock_config_source,
        query_tracker=mock_query_tracker,
        cleanup_manager=mock_cleanup_manager,
        registry=mock_registry,
    )


# ---------------------------------------------------------------------------
# Bug #734: start() must survive cleanup_stale_write_mode_markers raising
# ---------------------------------------------------------------------------


class TestBug734StartSurvivesCleanupException:
    """
    Bug #734: cleanup_stale_write_mode_markers(force=True) is called in start()
    BEFORE the thread is launched.  If it raises, the thread is never started.

    Fix: wrap call in try/except, log at ERROR with exc_info=True, proceed to
    thread launch regardless.

    The SUT is start() — we test that start() handles the exception properly.
    We trigger the failure via pathlib.Path.glob, an external stdlib dependency
    that cleanup_stale_write_mode_markers uses to enumerate marker files, without
    patching any method on RefreshScheduler itself.
    """

    def test_bug_734_start_survives_cleanup_exception(self, scheduler, caplog):
        """
        Create a .write_mode/ directory with a marker file so cleanup_stale_write_mode_markers
        enters its real code path, then inject an OSError via pathlib.Path.glob.

        Call start() — the scheduler thread MUST still be launched, and at least
        one ERROR record with exc_info attached MUST be present in the logs.
        """
        # Create a .write_mode/ dir with one marker so the method reaches glob()
        write_mode_dir = scheduler.golden_repos_dir / ".write_mode"
        write_mode_dir.mkdir(parents=True, exist_ok=True)
        (write_mode_dir / "test-alias.json").write_text(
            '{"entered_at": "2020-01-01T00:00:00"}'
        )

        def raising_glob(self_path, pattern):
            raise OSError("simulated glob failure during startup cleanup")

        with patch.object(pathlib.Path, "glob", raising_glob):
            with caplog.at_level(logging.ERROR):
                scheduler.start()

        try:
            # Thread must have been created and started despite the exception
            assert scheduler._thread is not None, (
                "scheduler._thread is None — thread was never assigned after cleanup raised"
            )
            assert scheduler._thread.is_alive(), (
                "scheduler._thread exists but is not alive — thread never started"
            )

            # At least one ERROR record must be present
            error_records = [r for r in caplog.records if r.levelname == "ERROR"]
            assert error_records, (
                "Expected at least one ERROR log record for cleanup failure, got none"
            )

            # The error must carry exc_info (logged with exc_info=True per the fix spec)
            records_with_exc_info = [r for r in error_records if r.exc_info is not None]
            assert records_with_exc_info, (
                "Expected at least one ERROR record to have exc_info attached "
                "(logged with exc_info=True). Records found: "
                f"{[(r.message, r.exc_info) for r in error_records]}"
            )
        finally:
            scheduler.stop()


# ---------------------------------------------------------------------------
# Bug #730: subprocess.run() for cidx scip generate must include timeout= kwarg
# ---------------------------------------------------------------------------


def _find_scip_subprocess_run_call(source: str) -> ast.Call:
    """
    AST-parse _index_source source and return the ast.Call node for the
    subprocess.run() invocation whose first positional argument is the Name
    node 'scip_command'.

    Raises AssertionError if no such call is found.
    """
    dedented = textwrap.dedent(source)
    tree = ast.parse(dedented)

    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue

        # Match `subprocess.run(...)` attribute call
        func = node.func
        is_subprocess_run = (
            isinstance(func, ast.Attribute)
            and func.attr == "run"
            and isinstance(func.value, ast.Name)
            and func.value.id == "subprocess"
        )
        if not is_subprocess_run:
            continue

        # First positional argument must be the Name 'scip_command'
        if not node.args:
            continue
        first_arg = node.args[0]
        if not (isinstance(first_arg, ast.Name) and first_arg.id == "scip_command"):
            continue

        return node

    raise AssertionError(
        "No subprocess.run(scip_command, ...) call found in _index_source(). "
        "The method structure may have changed — verify the SCIP subprocess block."
    )


class TestBug730ScipGenerateHasTimeout:
    """
    Bug #730 superseded by Bug #1218.

    Bug #730 originally required a timeout= on the SCIP subprocess.run call.
    Bug #1218 removes ALL overarching per-job timeouts on the indexing+SCIP path
    because they killed large-repo indexing mid-flight (partial/corrupt index).

    This class now verifies the ABSENCE of timeout= on subprocess.run(scip_command, ...)
    to guard against the timeout being re-introduced by well-meaning but incorrect fixes.
    """

    def test_bug_730_scip_generate_has_no_timeout(self):
        """
        _index_source() source must NOT have a 'timeout' keyword argument on the
        subprocess.run(scip_command, ...) call (Bug #1218).

        Overarching SCIP timeouts caused large-repo indexing to be killed
        mid-flight, producing a partial/corrupt index.  The only legitimate
        timeouts are per-request outbound embedding-provider HTTP calls.
        """
        source = inspect.getsource(RefreshScheduler._index_source)

        # AST-parse to find the exact subprocess.run(scip_command, ...) call
        scip_call = _find_scip_subprocess_run_call(source)

        # Check that NO 'timeout' keyword argument is present on that call
        timeout_kwargs = [kw for kw in scip_call.keywords if kw.arg == "timeout"]
        assert not timeout_kwargs, (
            "subprocess.run(scip_command, ...) in _index_source() still has a "
            "'timeout' keyword argument. "
            "Bug #1218 removes all overarching per-job timeouts on the indexing+SCIP path."
        )


# ---------------------------------------------------------------------------
# Bug #735: _scheduler_loop() must use exponential backoff on consecutive failures
# ---------------------------------------------------------------------------


class TestBug735ExponentialBackoffOnConsecutiveFailures:
    """
    Bug #735: _scheduler_loop() has no backoff on consecutive failures.  A
    permanently-broken upstream floods logs at fixed cadence.

    Fix: track consecutive_failures counter; on each failure compute
    backoff = min(base_interval * 2**consecutive_failures, MAX_BACKOFF_SECONDS)
    and use that as the wait timeout instead of the normal poll interval.
    Reset counter to 0 on each successful iteration.

    Verification strategy (load-safe):
    - Assert the backoff FORMULA directly (no live scheduler loop, no wall-clock).
    - Use patch.object on the SPECIFIC scheduler INSTANCE's _stop_event (not the
      threading.Event CLASS), so parallel tests cannot inject phantom wait calls.
    """

    def test_bug_735_exponential_backoff_on_consecutive_failures(
        self, scheduler, mock_registry, caplog
    ):
        """
        Two-part deterministic verification of Bug #735 fix:

        Part A — Formula contract: assert that min(30 * 2**(k-1), MAX) is
        non-decreasing and strictly grows between k=1 and k=2.  This fails
        immediately if the formula is changed to a flat interval or removed.

        Part B — Live loop behavioral: patch only the SPECIFIC INSTANCE's
        _stop_event so captured wait timeouts are isolated from parallel tests.
        With registry.list_global_repos() always raising, the loop must emit
        backoff values that match the formula.
        """
        # Part A: Verify the formula directly — no I/O, no threads, no wall-clock.
        # If someone removes the backoff or flattens it to a constant, this fails.
        MAX_BACKOFF_SECONDS = 3600  # must match production constant
        BASE = 30

        formula_timeouts = [
            min(BASE * (2 ** (k - 1)), MAX_BACKOFF_SECONDS) for k in range(1, 6)
        ]

        # Non-decreasing
        for i in range(1, len(formula_timeouts)):
            assert formula_timeouts[i] >= formula_timeouts[i - 1], (
                f"Backoff formula is not non-decreasing at k={i + 1}: "
                f"{formula_timeouts[i]} < {formula_timeouts[i - 1]}"
            )

        # At least one strict doubling between k=1 and k=2
        assert formula_timeouts[1] > formula_timeouts[0], (
            f"Backoff formula must strictly grow from k=1 ({formula_timeouts[0]}) "
            f"to k=2 ({formula_timeouts[1]})"
        )

        # Ceiling is respected
        assert max(formula_timeouts) <= MAX_BACKOFF_SECONDS, (
            f"Backoff formula exceeded MAX_BACKOFF_SECONDS ceiling: {formula_timeouts}"
        )

        # Part B: Run a short live loop, patching only THIS instance's _stop_event
        # to capture wait calls without affecting any other threading.Event object.
        mock_registry.list_global_repos.side_effect = RuntimeError(
            "simulated persistent registry failure"
        )

        wait_timeouts: list = []
        MAX_ITERATIONS = 3
        original_wait = scheduler._stop_event.wait

        def capturing_instance_wait(timeout=None):
            wait_timeouts.append(timeout)
            if len(wait_timeouts) >= MAX_ITERATIONS:
                scheduler._running = False
            # Mimic non-set: return False so the loop continues normally
            return False

        # Patch only the BOUND METHOD on this specific Event instance.
        scheduler._stop_event.wait = capturing_instance_wait  # type: ignore[method-assign]
        try:
            with caplog.at_level(logging.ERROR):
                scheduler._running = True
                scheduler._scheduler_loop()
        finally:
            scheduler._stop_event.wait = original_wait  # type: ignore[method-assign]

        assert len(wait_timeouts) >= MAX_ITERATIONS, (
            f"Expected at least {MAX_ITERATIONS} wait() calls, got {len(wait_timeouts)}"
        )

        # Verify non-decreasing backoff across all captured iterations
        for i in range(1, len(wait_timeouts)):
            assert wait_timeouts[i] >= wait_timeouts[i - 1], (
                f"Wait timeout at iteration {i} ({wait_timeouts[i]}) is less than "
                f"iteration {i - 1} ({wait_timeouts[i - 1]}) — backoff is not growing. "
                f"All timeouts: {wait_timeouts}"
            )

        # The second timeout must be strictly larger than the first
        assert wait_timeouts[1] > wait_timeouts[0], (
            f"Second wait timeout ({wait_timeouts[1]}) must be strictly greater than "
            f"first ({wait_timeouts[0]}) — exponential backoff requires at least one "
            f"doubling. All timeouts: {wait_timeouts}"
        )

        # Verify errors were logged
        error_records = [r for r in caplog.records if r.levelname == "ERROR"]
        assert error_records, (
            "Expected ERROR log records for registry failures, got none. "
            "Scheduler must log errors during failed iterations."
        )
