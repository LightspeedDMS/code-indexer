"""
Regression test for Bug #729 — UnboundLocalError on `refresh_interval` in
RefreshScheduler._scheduler_loop().

Root cause: `refresh_interval` was only assigned inside the `try` block (line 812).
If `list_global_repos()` or `get_refresh_interval()` raised before that assignment,
execution fell through to `_calculate_poll_interval(refresh_interval)` (outside the
try block) with `refresh_interval` unbound, silently killing the background thread.

Fix: initialize `refresh_interval = DEFAULT_REFRESH_INTERVAL` BEFORE the while loop.

Test design:
- Failures are driven through injected collaborators (registry, config_source).
- Thread lifecycle uses scheduler.start() / scheduler.stop() — public API only.
- No SUT internal state manipulation or monkey-patching.
"""

import logging
import time
from unittest.mock import Mock

import pytest

from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.refresh_scheduler import RefreshScheduler


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


def _start_then_stop(scheduler, settle_seconds: float = 0.05) -> None:
    """
    Start the scheduler via its public API, let it run one iteration, then stop it.

    The collaborator is configured to raise immediately, so the try block exits
    fast and the loop reaches _stop_event.wait().  We wait `settle_seconds` for
    that to happen, then call stop() which sets the event and joins the thread.
    """
    scheduler.start()
    time.sleep(settle_seconds)
    scheduler.stop()


def assert_error_logged(
    caplog: pytest.LogCaptureFixture, expected_fragment: str
) -> None:
    """Assert at least one ERROR record contains `expected_fragment` verbatim."""
    error_records = [r for r in caplog.records if r.levelname == "ERROR"]
    assert error_records, (
        f"Expected at least one ERROR log record. "
        f"All records: {[r.message for r in caplog.records]}"
    )
    matching = [r for r in error_records if expected_fragment in r.message]
    assert matching, (
        f"No ERROR record contained {expected_fragment!r}. "
        f"ERROR messages: {[r.message for r in error_records]}"
    )


@pytest.mark.filterwarnings("error::pytest.PytestUnhandledThreadExceptionWarning")
class TestBug729UnboundRefreshInterval:
    """
    Verify that _scheduler_loop() does NOT raise UnboundLocalError when a
    collaborator raises before `refresh_interval` is assigned.

    The filterwarnings marker promotes PytestUnhandledThreadExceptionWarning to an
    error so that UnboundLocalError escaping the background thread causes a real test
    FAILURE (red state) rather than just a warning.
    """

    def test_no_unbound_error_when_list_global_repos_raises(
        self, scheduler, mock_registry, caplog
    ):
        """
        Bug #729 regression: registry.list_global_repos() raising before
        get_refresh_interval() must not cause UnboundLocalError.
        The exception must be caught and logged; the thread must exit cleanly.
        """
        mock_registry.list_global_repos.side_effect = RuntimeError(
            "simulated registry failure"
        )

        with caplog.at_level(logging.ERROR):
            _start_then_stop(scheduler)

        assert_error_logged(caplog, "simulated registry failure")

    def test_no_unbound_error_when_config_source_raises(
        self, scheduler, mock_config_source, caplog
    ):
        """
        Bug #729 regression: config_source.get_global_refresh_interval() raising
        (which get_refresh_interval() delegates to for non-GlobalRepoOperations sources)
        must not cause UnboundLocalError.
        """
        mock_config_source.get_global_refresh_interval.side_effect = RuntimeError(
            "config source unavailable"
        )

        with caplog.at_level(logging.ERROR):
            _start_then_stop(scheduler)

        assert_error_logged(caplog, "config source unavailable")
        mock_config_source.get_global_refresh_interval.assert_called()
