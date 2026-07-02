"""
Unit tests for Bug #953: circuit-breaker for infinite reschedule loop.

When a golden repo's descriptor file lacks YAML frontmatter or is missing
description/last_analyzed fields, _get_refresh_prompt() returns None and
_run_loop_single_pass() reschedules the repo -- but the missing fields are
never populated, so the cycle never terminates.

Fix: per-repo consecutive-failure counter (defaultdict(int)) on the scheduler.
After N=3 consecutive failures the repo is quarantined (not rescheduled) and
ONE ERROR-level log is emitted.  Counter resets to 0 on any successful refresh.

Anti-mock strategy: only injected dependencies (tracking_backend, golden_backend,
config_manager, claude_cli_manager) are mocked.  The scheduler's own methods
run real code.  A stale repo is produced by having tracking_backend return a row
with next_run in the past.  The prompt fails naturally because _meta_dir is left
unset (None), so _read_existing_description() logs a warning and returns None.

Test inventory:
    TestCircuitBreakerQuarantine
        test_repo_quarantined_on_nth_failure_not_rescheduled
        test_repo_not_quarantined_after_n_minus_one_failures
    TestCircuitBreakerReset
        test_counter_resets_to_zero_after_success
        test_after_reset_repo_is_rescheduled_on_next_failure
        test_after_reset_repo_quarantines_again_on_nth_failure
    TestCircuitBreakerLogging
        test_exactly_one_error_logged_on_quarantine
        test_no_additional_error_logged_after_quarantine_established
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock

_QUARANTINE_THRESHOLD = 3  # must match PROMPT_FAILURE_QUARANTINE_THRESHOLD in scheduler
_PAST_TIME = "2000-01-01T00:00:00+00:00"  # always stale -- triggers get_stale_repos
_REFRESH_INTERVAL_HOURS = 24  # bucket count for calculate_next_run()


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_scheduler(tmp_path):
    """
    Construct DescriptionRefreshScheduler with injectable backends.

    tracking_backend returns one stale repo row (next_run in the past).
    golden_backend returns the matching clone_path.
    config_manager returns a minimal config so calculate_next_run() works.
    claude_cli_manager is required so _run_loop_single_pass() enters the
    prompt-generation branch (without it the scheduler skips silently).
    _meta_dir is intentionally not set so _get_refresh_prompt() returns None
    naturally via _read_existing_description() -> "meta directory not set".

    Returns (scheduler, tracking_backend, alias, clone_path).
    """
    from code_indexer.server.services.description_refresh_scheduler import (
        DescriptionRefreshScheduler,
    )
    from code_indexer.server.utils.config_manager import (
        ClaudeIntegrationConfig,
        ServerConfig,
    )

    alias = "broken-repo"
    clone_path = str(tmp_path)

    stale_row = {
        "repo_alias": alias,
        "clone_path": clone_path,
        "next_run": _PAST_TIME,
        "status": "pending",
    }

    tracking_backend = MagicMock(name="tracking_backend")
    tracking_backend.get_stale_repos.return_value = [stale_row]

    golden_backend = MagicMock(name="golden_backend")
    golden_backend.get_repo.return_value = {"clone_path": clone_path}

    config = ServerConfig(server_dir=str(tmp_path))
    config.claude_integration_config = ClaudeIntegrationConfig()
    config.claude_integration_config.description_refresh_enabled = True
    config.claude_integration_config.description_refresh_interval_hours = (
        _REFRESH_INTERVAL_HOURS
    )

    config_manager = MagicMock(name="config_manager")
    config_manager.load_config.return_value = config

    scheduler = DescriptionRefreshScheduler(
        tracking_backend=tracking_backend,
        golden_backend=golden_backend,
        config_manager=config_manager,
        claude_cli_manager=MagicMock(name="claude_cli_manager"),
        # _meta_dir intentionally omitted -- prompt returns None naturally
    )
    return scheduler, tracking_backend, alias, clone_path


def _was_repo_rescheduled(tracking_backend: MagicMock, alias: str) -> bool:
    """Return True if upsert_tracking was called for *alias* on the last pass."""
    return any(
        call_.kwargs.get("repo_alias") == alias
        or (len(call_.args) > 0 and call_.args[0] == alias)
        for call_ in tracking_backend.upsert_tracking.call_args_list
    )


def _run_one_failing_pass(scheduler, tracking_backend) -> None:
    """Reset the upsert mock and run one _run_loop_single_pass()."""
    tracking_backend.upsert_tracking.reset_mock()
    scheduler._run_loop_single_pass()


def _run_n_failing_passes(scheduler, tracking_backend, n: int) -> None:
    """Run *n* failing passes, resetting the upsert mock before each."""
    for _ in range(n):
        _run_one_failing_pass(scheduler, tracking_backend)


def _collect_quarantine_errors(
    scheduler, tracking_backend, alias: str, passes: int
) -> list:
    """Run *passes* failing passes and return quarantine ERROR log records for *alias*."""
    error_records: list = []
    module_logger = logging.getLogger(
        "code_indexer.server.services.description_refresh_scheduler"
    )

    class _ErrorCapture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if (
                record.levelno >= logging.ERROR
                and alias in record.getMessage()
                and "quarantine" in record.getMessage().lower()
            ):
                error_records.append(record)

    handler = _ErrorCapture()
    module_logger.addHandler(handler)
    try:
        _run_n_failing_passes(scheduler, tracking_backend, passes)
    finally:
        module_logger.removeHandler(handler)
    return error_records


# ---------------------------------------------------------------------------
# TestCircuitBreakerQuarantine
# ---------------------------------------------------------------------------


class TestCircuitBreakerQuarantine:
    """Verify that a repo is quarantined on the Nth consecutive prompt failure."""

    def test_repo_quarantined_on_nth_failure_not_rescheduled(self, tmp_path):
        """
        On the Nth consecutive None-prompt failure the scheduler must NOT call
        upsert_tracking for that repo (quarantine fires on the Nth run itself).
        """
        scheduler, tracking_backend, alias, _ = _make_scheduler(tmp_path)

        for i in range(_QUARANTINE_THRESHOLD - 1):
            _run_one_failing_pass(scheduler, tracking_backend)
            assert _was_repo_rescheduled(tracking_backend, alias), (
                f"Repo must be rescheduled on failure #{i + 1} "
                f"(threshold={_QUARANTINE_THRESHOLD})"
            )

        # Nth pass: quarantine fires -- must NOT reschedule
        _run_one_failing_pass(scheduler, tracking_backend)

        assert not _was_repo_rescheduled(tracking_backend, alias), (
            f"Quarantined repo '{alias}' must NOT be rescheduled on the "
            f"{_QUARANTINE_THRESHOLD}th consecutive failure"
        )

    def test_repo_not_quarantined_after_n_minus_one_failures(self, tmp_path):
        """
        After N-1 consecutive failures the repo must still be rescheduled;
        quarantine must not fire before the Nth failure.
        """
        scheduler, tracking_backend, alias, _ = _make_scheduler(tmp_path)

        for i in range(_QUARANTINE_THRESHOLD - 1):
            _run_one_failing_pass(scheduler, tracking_backend)
            assert _was_repo_rescheduled(tracking_backend, alias), (
                f"Repo must still be rescheduled on failure #{i + 1} "
                f"(threshold is {_QUARANTINE_THRESHOLD})"
            )


# ---------------------------------------------------------------------------
# TestCircuitBreakerReset
# ---------------------------------------------------------------------------


class TestCircuitBreakerReset:
    """Verify counter resets to zero on success and quarantine re-arms correctly."""

    def test_counter_resets_to_zero_after_success(self, tmp_path):
        """on_refresh_complete(success=True) resets _prompt_failure_counts to 0."""
        scheduler, tracking_backend, alias, clone_path = _make_scheduler(tmp_path)

        _run_n_failing_passes(scheduler, tracking_backend, _QUARANTINE_THRESHOLD - 1)

        assert scheduler._prompt_failure_counts[alias] == _QUARANTINE_THRESHOLD - 1, (
            f"Expected failure count {_QUARANTINE_THRESHOLD - 1}, "
            f"got {scheduler._prompt_failure_counts[alias]}"
        )

        scheduler.on_refresh_complete(alias, clone_path, success=True)

        assert scheduler._prompt_failure_counts[alias] == 0, (
            f"Failure counter must reset to 0 after success, "
            f"got {scheduler._prompt_failure_counts[alias]}"
        )

    def test_after_reset_repo_is_rescheduled_on_next_failure(self, tmp_path):
        """After a reset, the first subsequent failure must reschedule (not quarantine)."""
        scheduler, tracking_backend, alias, clone_path = _make_scheduler(tmp_path)

        _run_n_failing_passes(scheduler, tracking_backend, _QUARANTINE_THRESHOLD - 1)
        scheduler.on_refresh_complete(alias, clone_path, success=True)

        _run_one_failing_pass(scheduler, tracking_backend)

        assert _was_repo_rescheduled(tracking_backend, alias), (
            "After counter reset, the first new failure must reschedule the repo"
        )

    def test_after_reset_repo_quarantines_again_on_nth_failure(self, tmp_path):
        """After a reset and N new failures, the repo must be quarantined again."""
        scheduler, tracking_backend, alias, clone_path = _make_scheduler(tmp_path)

        _run_n_failing_passes(scheduler, tracking_backend, _QUARANTINE_THRESHOLD - 1)
        scheduler.on_refresh_complete(alias, clone_path, success=True)

        _run_n_failing_passes(scheduler, tracking_backend, _QUARANTINE_THRESHOLD - 1)
        _run_one_failing_pass(scheduler, tracking_backend)

        assert not _was_repo_rescheduled(tracking_backend, alias), (
            f"After counter reset and {_QUARANTINE_THRESHOLD} new failures, "
            "repo must be quarantined again"
        )


# ---------------------------------------------------------------------------
# TestCircuitBreakerLogging
# ---------------------------------------------------------------------------


class TestCircuitBreakerLogging:
    """Verify that the quarantine boundary emits exactly one ERROR log."""

    def test_exactly_one_error_logged_on_quarantine(self, tmp_path):
        """Exactly one ERROR quarantine log must be emitted when the threshold is hit."""
        scheduler, tracking_backend, alias, _ = _make_scheduler(tmp_path)

        errors = _collect_quarantine_errors(
            scheduler, tracking_backend, alias, _QUARANTINE_THRESHOLD
        )

        assert len(errors) == 1, (
            f"Expected exactly 1 quarantine ERROR log, got {len(errors)}: "
            f"{[r.getMessage() for r in errors]}"
        )

    def test_no_additional_error_logged_after_quarantine_established(self, tmp_path):
        """No further quarantine ERROR logs after the repo is already quarantined."""
        scheduler, tracking_backend, alias, _ = _make_scheduler(tmp_path)

        _collect_quarantine_errors(
            scheduler, tracking_backend, alias, _QUARANTINE_THRESHOLD
        )

        post_errors = _collect_quarantine_errors(
            scheduler, tracking_backend, alias, _QUARANTINE_THRESHOLD
        )

        assert len(post_errors) == 0, (
            f"No additional quarantine ERROR logs expected after quarantine established, "
            f"got {len(post_errors)}"
        )
