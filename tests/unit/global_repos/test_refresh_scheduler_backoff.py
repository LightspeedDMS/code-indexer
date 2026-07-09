"""
Unit tests for RefreshScheduler exponential backoff on fetch failures (Bug #1341).

Corrected design (per explicit user direction): a repo is NEVER removed from
scheduling and NEVER reaches a terminal/quarantine state, no matter how the
fetch error is classified. A TRANSIENT error is expected to recover on its
own, so it keeps its existing immediate-retry + re-clone-escalation
behavior completely unchanged. A PERMANENT error (access revoked, repo
deleted -- GitLab/GitHub "not found / no permission") is NOT expected to
recover quickly, so instead of hammering it every cycle forever (the
original #1341 log-flood complaint), the scheduler pushes its next
scheduled attempt further and further into the future using exponential
backoff, capped at a long-but-finite interval -- it is always retried
eventually, just less often while broken.

ERROR-level logging is throttled to power-of-two milestones of the
consecutive-failure count (1, 2, 4, 8, 16, ...) instead of every single
cycle, directly fixing the "one ERROR set per cycle, indefinitely" flood
described in the bug report.

Tests verify:
  - A permanent fetch error's backoff grows exponentially then caps at
    PERMANENT_BACKOFF_CAP_SECONDS; it is pushed via registry.update_next_refresh
    to a bounded future time -- never skipped/removed from scheduling.
  - A transient fetch error keeps immediate retry (no backoff override) for
    the first MAX_TRANSIENT_FAILURES-1 failures, still escalates to
    re-clone at the threshold exactly as before, and only backs off (base
    60s, capped at TRANSIENT_BACKOFF_CAP_SECONDS) once sustained past the
    threshold.
  - ERROR logging is milestone-throttled for both categories -- not logged
    on every cycle.
  - A subsequent successful fetch resets the failure counter, which resets
    backoff to normal cadence (recovery).
"""

import logging
import time
import pytest
from unittest.mock import Mock, patch

from code_indexer.global_repos.refresh_scheduler import RefreshScheduler
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.global_repos.cleanup_manager import CleanupManager
from code_indexer.global_repos.git_error_classifier import (
    GitFetchError,
    classify_fetch_error,
)


ALIAS = "sales-global"
REPO_URL = "https://gitlab.example.com/group/sales.git"
MASTER_PATH = "/fake/golden-repos/sales"

GITLAB_PERMANENT_STDERR = (
    "remote: ERROR: The project you were looking for could not be found "
    "or you don't have permission to view it.\n"
    "fatal: Could not read from remote repository.\n\n"
    "Please make sure you have the correct access rights and the "
    "repository exists.\n"
)


@pytest.fixture
def mock_golden_repos_dir(tmp_path):
    golden_dir = tmp_path / "golden-repos"
    golden_dir.mkdir()
    return str(golden_dir)


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
    registry.get_global_repo.return_value = {
        "alias_name": ALIAS,
        "repo_url": REPO_URL,
        "enable_temporal": False,
        "enable_scip": False,
    }
    return registry


@pytest.fixture
def scheduler(
    mock_golden_repos_dir,
    mock_config_source,
    mock_query_tracker,
    mock_cleanup_manager,
    mock_registry,
):
    return RefreshScheduler(
        golden_repos_dir=mock_golden_repos_dir,
        config_source=mock_config_source,
        query_tracker=mock_query_tracker,
        cleanup_manager=mock_cleanup_manager,
        registry=mock_registry,
    )


def _permanent_error() -> GitFetchError:
    category = classify_fetch_error(GITLAB_PERMANENT_STDERR)
    assert category == "permanent"  # sanity: real classifier, no mocks
    return GitFetchError(
        "Git fetch failed", category=category, stderr=GITLAB_PERMANENT_STDERR
    )


def _transient_error() -> GitFetchError:
    stderr = "ssh: connect to host example.com port 22: Connection timed out\n"
    category = classify_fetch_error(stderr)
    assert category == "transient"  # sanity: real classifier, no mocks
    return GitFetchError("Git fetch failed", category=category, stderr=stderr)


class TestPermanentErrorBackoff:
    def test_permanent_error_never_reclones(self, scheduler):
        error = _permanent_error()
        with patch.object(scheduler, "_attempt_reclone") as mock_reclone:
            with pytest.raises(RuntimeError):
                scheduler._handle_fetch_error(ALIAS, REPO_URL, MASTER_PATH, error)
        mock_reclone.assert_not_called()

    def test_permanent_error_backoff_grows_exponentially(self, scheduler):
        error = _permanent_error()
        base = scheduler.PERMANENT_BACKOFF_BASE_SECONDS
        assert scheduler._compute_backoff_seconds(error.category, 1) == base
        assert scheduler._compute_backoff_seconds(error.category, 2) == base * 2
        assert scheduler._compute_backoff_seconds(error.category, 3) == base * 4
        assert scheduler._compute_backoff_seconds(error.category, 4) == base * 8

    def test_permanent_error_backoff_caps_at_ceiling(self, scheduler):
        error = _permanent_error()
        cap = scheduler.PERMANENT_BACKOFF_CAP_SECONDS
        # Growth exceeds the cap well before count=20; must clamp, not overflow.
        assert scheduler._compute_backoff_seconds(error.category, 20) == cap
        assert scheduler._compute_backoff_seconds(error.category, 100) == cap

    def test_permanent_error_is_always_rescheduled_never_dropped(self, scheduler):
        """The repo must NEVER be removed from scheduling -- every permanent
        failure pushes next_refresh to a bounded future timestamp via the
        registry, proving it stays in the schedule."""
        error = _permanent_error()
        before = time.time()
        with pytest.raises(RuntimeError):
            scheduler._handle_fetch_error(ALIAS, REPO_URL, MASTER_PATH, error)

        scheduler.registry.update_next_refresh.assert_called_once()
        call_alias, call_next_refresh = (
            scheduler.registry.update_next_refresh.call_args[0]
        )
        assert call_alias == ALIAS
        # Scheduled strictly in the future, bounded by the cap (never "never").
        assert (
            before
            < call_next_refresh
            <= before + scheduler.PERMANENT_BACKOFF_CAP_SECONDS + 5
        )

    def test_permanent_error_error_log_throttled_to_milestones(self, scheduler, caplog):
        error = _permanent_error()
        with patch.object(scheduler, "_attempt_reclone"):
            with caplog.at_level(logging.DEBUG):
                for _ in range(10):
                    with pytest.raises(RuntimeError):
                        scheduler._handle_fetch_error(
                            ALIAS, REPO_URL, MASTER_PATH, error
                        )

        error_logs = [r for r in caplog.records if r.levelname == "ERROR"]
        # Milestones (power-of-two) within 1..10: {1, 2, 4, 8} = 4 ERROR logs,
        # NOT 10 (one per cycle) -- this is the log-flood fix.
        assert len(error_logs) == 4, (
            f"Expected exactly 4 milestone ERROR logs across 10 cycles, got "
            f"{len(error_logs)}: {[r.message for r in error_logs]}"
        )

    def test_permanent_error_recovers_to_base_backoff_after_success(self, scheduler):
        error = _permanent_error()
        for _ in range(5):
            with pytest.raises(RuntimeError):
                scheduler._handle_fetch_error(ALIAS, REPO_URL, MASTER_PATH, error)
        assert scheduler._fetch_failure_counts[ALIAS] == 5

        # A successful fetch resets the counter (and therefore the backoff).
        scheduler._reset_fetch_failures(ALIAS)
        assert scheduler._fetch_failure_counts[ALIAS] == 0

        with pytest.raises(RuntimeError):
            scheduler._handle_fetch_error(ALIAS, REPO_URL, MASTER_PATH, error)
        assert scheduler._fetch_failure_counts[ALIAS] == 1
        assert (
            scheduler._compute_backoff_seconds(error.category, 1)
            == scheduler.PERMANENT_BACKOFF_BASE_SECONDS
        )


class TestTransientErrorBackoffUnaffectedUntilSustained:
    def test_transient_failure_below_threshold_has_no_backoff_override(self, scheduler):
        """Immediate-retry cadence preserved: below MAX_TRANSIENT_FAILURES,
        _handle_fetch_error must NOT touch next_refresh at all -- the normal
        scheduler-loop cadence applies unchanged."""
        error = _transient_error()
        with patch.object(scheduler, "_attempt_reclone") as mock_reclone:
            for _ in range(scheduler.MAX_TRANSIENT_FAILURES - 1):
                with pytest.raises(RuntimeError):
                    scheduler._handle_fetch_error(ALIAS, REPO_URL, MASTER_PATH, error)

        mock_reclone.assert_not_called()
        scheduler.registry.update_next_refresh.assert_not_called()

    def test_transient_failure_still_escalates_to_reclone_at_threshold(self, scheduler):
        error = _transient_error()
        with patch.object(
            scheduler, "_attempt_reclone", return_value=True
        ) as mock_reclone:
            for _ in range(scheduler.MAX_TRANSIENT_FAILURES):
                scheduler._reclone_cooldowns[ALIAS] = 0.0  # bypass cooldown guard
                with pytest.raises(RuntimeError):
                    scheduler._handle_fetch_error(ALIAS, REPO_URL, MASTER_PATH, error)

        # Unchanged pre-existing behavior: re-clone attempted exactly once
        # the count reaches MAX_TRANSIENT_FAILURES.
        mock_reclone.assert_called_once_with(ALIAS, REPO_URL, MASTER_PATH)

    def test_transient_failure_keeps_retrying_indefinitely_past_threshold(
        self, scheduler
    ):
        """Regression guard: transient errors are NEVER quarantined/dropped --
        the handler keeps incrementing the counter and re-raising forever."""
        error = _transient_error()
        with patch.object(scheduler, "_attempt_reclone", return_value=False):
            for _ in range(30):
                scheduler._reclone_cooldowns[ALIAS] = 0.0
                with pytest.raises(RuntimeError):
                    scheduler._handle_fetch_error(ALIAS, REPO_URL, MASTER_PATH, error)

        assert scheduler._fetch_failure_counts[ALIAS] == 30

    def test_transient_backoff_grows_then_caps_once_sustained(self, scheduler):
        threshold = scheduler.MAX_TRANSIENT_FAILURES
        base = scheduler.TRANSIENT_BACKOFF_CAP_SECONDS  # cap reference
        assert scheduler._compute_backoff_seconds("transient", threshold - 1) is None
        first = scheduler._compute_backoff_seconds("transient", threshold)
        second = scheduler._compute_backoff_seconds("transient", threshold + 1)
        assert first == scheduler.TRANSIENT_BACKOFF_BASE_SECONDS
        assert second == scheduler.TRANSIENT_BACKOFF_BASE_SECONDS * 2
        assert scheduler._compute_backoff_seconds("transient", threshold + 20) == base

    def test_transient_error_log_throttled_to_milestones_once_sustained(
        self, scheduler, caplog
    ):
        error = _transient_error()
        with patch.object(scheduler, "_attempt_reclone", return_value=False):
            with caplog.at_level(logging.DEBUG):
                for _ in range(15):
                    scheduler._reclone_cooldowns[ALIAS] = 0.0
                    with pytest.raises(RuntimeError):
                        scheduler._handle_fetch_error(
                            ALIAS, REPO_URL, MASTER_PATH, error
                        )

        error_logs = [r for r in caplog.records if r.levelname == "ERROR"]
        # 15 cycles total; first 2 are below threshold (WARNING only, no
        # ERROR). From count=3 (threshold) the "escalating" ERROR is
        # milestone-throttled: relative index 1..13, power-of-two
        # milestones {1,2,4,8} = 4 ERROR logs, NOT 13.
        assert len(error_logs) == 4, (
            f"Expected exactly 4 milestone ERROR logs, got {len(error_logs)}: "
            f"{[r.message for r in error_logs]}"
        )


class TestCorruptionCategoryUnaffected:
    def test_corruption_still_reclones_every_call_no_backoff(self, scheduler):
        """Out of scope for #1341 -- corruption behavior must stay byte-identical:
        immediate re-clone attempt on every call (gated only by the pre-existing
        cooldown), no backoff override."""
        error = GitFetchError(
            "Git fetch failed", category="corruption", stderr="pack has bad object"
        )
        with patch.object(scheduler, "_attempt_reclone") as mock_reclone:
            with pytest.raises(RuntimeError):
                scheduler._handle_fetch_error(ALIAS, REPO_URL, MASTER_PATH, error)
        mock_reclone.assert_called_once_with(ALIAS, REPO_URL, MASTER_PATH)
        scheduler.registry.update_next_refresh.assert_not_called()
