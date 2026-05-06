"""
Unit tests for Bug #984: warning deduplication in DescriptionRefreshScheduler.

After v10.4.9 fixed the upstream cause of cidx-meta filename mismatch wipes,
the warning rate dropped 94% but 4 residual warnings per 30 min remained.
These come from repos with stub descriptions (missing last_analyzed): each
scheduler pass calls _get_refresh_prompt() which emits a WARNING before the
quarantine-check branch can suppress it.

Fix (v10.4.11):
  Part 1 - Move quarantine check BEFORE _get_refresh_prompt() in
           _run_loop_single_pass(), so already-quarantined repos never reach
           the prompt-generation code and its warning.
  Part 2 - _get_refresh_prompt() uses a per-instance _warned_missing_desc set
           to emit the "missing description or last_analyzed" warning at WARNING
           level only once per repo per scheduler lifetime; subsequent passes
           downgrade to DEBUG.  Re-armed on successful refresh.

Anti-mock strategy: mirrors test_description_refresh_scheduler_bug953.py.
Only injected dependencies are mocked.  Real scheduler methods run.
_meta_dir is intentionally omitted so _read_existing_description() returns
None naturally, causing _get_refresh_prompt() to hit the warning branch.

Test inventory:
    test_warning_emitted_once_per_repo_per_lifetime
    test_warning_re_armed_after_successful_refresh
    test_quarantine_check_short_circuits_get_refresh_prompt
    test_legit_first_warning_still_emitted
"""

from __future__ import annotations

import logging
from unittest.mock import MagicMock, patch

_QUARANTINE_THRESHOLD = 3  # must match PROMPT_FAILURE_QUARANTINE_THRESHOLD
_PAST_TIME = "2000-01-01T00:00:00+00:00"
_REFRESH_INTERVAL_HOURS = 24


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_scheduler(tmp_path):
    """
    Construct DescriptionRefreshScheduler with injectable backends.

    tracking_backend returns one stale repo row (next_run in the past).
    golden_backend returns the matching clone_path.
    _meta_dir is intentionally not set so _get_refresh_prompt() returns None
    naturally, reaching the "missing description or last_analyzed" branch.

    Returns (scheduler, tracking_backend, alias, clone_path).
    """
    from code_indexer.server.services.description_refresh_scheduler import (
        DescriptionRefreshScheduler,
    )
    from code_indexer.server.utils.config_manager import (
        ClaudeIntegrationConfig,
        ServerConfig,
    )

    alias = "stub-repo"
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


def _collect_warnings(scheduler, passes: int, alias: str) -> list:
    """
    Run *passes* single-pass iterations and return all WARNING-level log records
    containing "missing description or last_analyzed" for *alias*.
    """
    module_logger = logging.getLogger(
        "code_indexer.server.services.description_refresh_scheduler"
    )
    warning_records: list = []

    class _WarningCapture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            if (
                record.levelno == logging.WARNING
                and "missing description or last_analyzed" in record.getMessage()
                and alias in record.getMessage()
            ):
                warning_records.append(record)

    handler = _WarningCapture()
    module_logger.addHandler(handler)
    try:
        for _ in range(passes):
            scheduler._run_loop_single_pass()
    finally:
        module_logger.removeHandler(handler)
    return warning_records


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_warning_emitted_once_per_repo_per_lifetime(tmp_path):
    """
    Running 3 scheduler passes for a stub repo (missing last_analyzed) must
    emit the "missing description or last_analyzed" WARNING exactly once —
    not once per pass.
    """
    scheduler, _, alias, _ = _make_scheduler(tmp_path)

    warnings = _collect_warnings(scheduler, passes=3, alias=alias)

    assert len(warnings) == 1, (
        f"Expected exactly 1 WARNING for '{alias}' across 3 passes, "
        f"got {len(warnings)}: {[r.getMessage() for r in warnings]}"
    )


def test_warning_re_armed_after_successful_refresh(tmp_path):
    """
    After a successful refresh clears the warned flag, the next pass that
    encounters a missing-description condition must emit the WARNING again
    (re-armed), and only once after re-arming.
    """
    scheduler, _, alias, clone_path = _make_scheduler(tmp_path)

    # First pass: warning fires (1st time)
    initial_warnings = _collect_warnings(scheduler, passes=1, alias=alias)
    assert len(initial_warnings) == 1, (
        f"Expected 1 initial WARNING, got {len(initial_warnings)}"
    )

    # Simulate a successful refresh — re-arms the warned flag
    scheduler.on_refresh_complete(alias, clone_path, success=True)
    assert alias not in scheduler._warned_missing_desc, (
        "_warned_missing_desc must be cleared after a successful refresh"
    )

    # Next pass after re-arm: warning fires again (exactly once)
    post_rearmed_warnings = _collect_warnings(scheduler, passes=2, alias=alias)
    assert len(post_rearmed_warnings) == 1, (
        f"Expected exactly 1 re-armed WARNING across 2 passes after success, "
        f"got {len(post_rearmed_warnings)}"
    )


def test_quarantine_check_short_circuits_get_refresh_prompt(tmp_path):
    """
    When a repo's _prompt_failure_counts >= PROMPT_FAILURE_QUARANTINE_THRESHOLD,
    _run_loop_single_pass() must skip the repo without calling _get_refresh_prompt()
    at all.
    """
    scheduler, _, alias, _ = _make_scheduler(tmp_path)

    # Manually set the failure count to quarantine threshold
    from code_indexer.server.services.description_refresh_scheduler import (
        PROMPT_FAILURE_QUARANTINE_THRESHOLD,
    )

    scheduler._prompt_failure_counts[alias] = PROMPT_FAILURE_QUARANTINE_THRESHOLD

    with patch.object(
        scheduler, "_get_refresh_prompt", wraps=scheduler._get_refresh_prompt
    ) as mock_prompt:
        scheduler._run_loop_single_pass()
        (
            mock_prompt.assert_not_called(),
            ("_get_refresh_prompt must NOT be called for a quarantined repo"),
        )


def test_legit_first_warning_still_emitted(tmp_path):
    """
    Regression: the first time a stub repo (missing last_analyzed) is encountered,
    the WARNING must still fire at WARNING level — it must not be silently dropped.
    """
    scheduler, _, alias, _ = _make_scheduler(tmp_path)

    # Fresh scheduler: _warned_missing_desc is empty for this alias
    assert alias not in scheduler._warned_missing_desc, (
        "Precondition: alias must not be in _warned_missing_desc before first pass"
    )

    warnings = _collect_warnings(scheduler, passes=1, alias=alias)

    assert len(warnings) == 1, (
        f"First-pass WARNING must fire at WARNING level for '{alias}', "
        f"got {len(warnings)} warnings"
    )
    assert alias in scheduler._warned_missing_desc, (
        "After first WARNING, alias must be recorded in _warned_missing_desc"
    )
