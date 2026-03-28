"""
Unit tests for activity journal logging in run_refinement_cycle().

Tests:
  1. _activity_journal.init() is called during refinement cycle
  2. _activity_journal.log("Starting refinement cycle") is called
  3. Per-domain log messages are emitted for each domain in the batch
  4. Completion log message is emitted after cursor update
  5. Journal errors do not break refinement (non-fatal)
  6. Journal init uses the correct directory path (~/.tmp/depmap-refinement-journal/)

The pattern under test (must match exactly):
    try:
        self._activity_journal.init(journal_dir)
        self._activity_journal.log("Starting refinement cycle")
    except Exception as e:
        logger.debug(f"Non-fatal journal init error: {e}")

    try:
        self._activity_journal.log("...")
    except Exception as e:
        logger.debug(f"Non-fatal journal log error: {e}")

These tests call run_refinement_cycle() directly (NOT mocked) to test the real
implementation behavior. All collaborators are mocked to avoid real I/O:
- _activity_journal (mock to capture calls)
- _config_manager
- _tracking_backend
- _golden_repos_manager
- _lock
- _refresh_scheduler
- filesystem operations (via temporary directory)
"""

import json
import os
from pathlib import Path
from unittest.mock import MagicMock


# ─────────────────────────────────────────────────────────────────────────────
# Helpers
# ─────────────────────────────────────────────────────────────────────────────


def _make_config(refinement_enabled: bool = True, domains_per_run: int = 2):
    """Build a mock claude integration config."""
    cfg = MagicMock()
    cfg.refinement_enabled = refinement_enabled
    cfg.refinement_domains_per_run = domains_per_run
    return cfg


def _make_service(
    tmp_path: Path,
    domains: list,
    refinement_enabled: bool = True,
    domains_per_run: int = 2,
):
    """
    Build a DependencyMapService with mocked internals pointing at tmp_path.

    Sets up:
    - A real _domains.json with `domains` content in tmp_path
    - Mock _activity_journal (spy-style - records calls, doesn't raise)
    - Mock _config_manager returning refinement_enabled and domains_per_run
    - Mock _tracking_backend (cursor=0)
    - Mock _golden_repos_manager
    - Real threading.Lock() replaced with a mock that always acquires
    - Mock _refresh_scheduler
    - Mock refine_or_create_domain returning changed=False by default
    """
    from code_indexer.server.services.dependency_map_service import DependencyMapService

    # --- file system setup ---
    dep_map_read_dir = tmp_path / "dependency-map"
    dep_map_read_dir.mkdir(parents=True, exist_ok=True)
    domains_json_path = dep_map_read_dir / "_domains.json"
    domains_json_path.write_text(json.dumps(domains))

    dep_map_write_dir = tmp_path / "write" / "dependency-map"
    dep_map_write_dir.mkdir(parents=True, exist_ok=True)

    # --- config mock ---
    mock_config = _make_config(refinement_enabled, domains_per_run)
    mock_config_manager = MagicMock()
    mock_config_manager.get_claude_integration_config.return_value = mock_config

    # --- tracking mock (cursor 0) ---
    mock_tracking = MagicMock()
    mock_tracking.get_tracking.return_value = {"refinement_cursor": 0}
    mock_tracking.update_tracking.return_value = None

    # --- golden repos manager mock ---
    mock_golden_repos = MagicMock()
    mock_golden_repos.golden_repos_dir = str(tmp_path / "write")

    # --- analyzer mock ---
    mock_analyzer = MagicMock()
    mock_analyzer._generate_index_md.return_value = None

    service = DependencyMapService(
        golden_repos_manager=mock_golden_repos,
        config_manager=mock_config_manager,
        tracking_backend=mock_tracking,
        analyzer=mock_analyzer,
        refresh_scheduler=None,
        job_tracker=None,
    )

    # Replace journal with a mock (spy-style, records all calls)
    mock_journal = MagicMock()
    mock_journal.init.return_value = None
    mock_journal.log.return_value = None
    service._activity_journal = mock_journal

    # Replace lock with one that always succeeds (non-blocking acquire)
    mock_lock = MagicMock()
    mock_lock.acquire.return_value = True
    mock_lock.release.return_value = None
    service._lock = mock_lock

    # Patch _get_cidx_meta_read_path to return tmp_path
    service._get_cidx_meta_read_path = MagicMock(return_value=tmp_path)

    # Patch refine_or_create_domain to return False (no changes) by default
    service.refine_or_create_domain = MagicMock(return_value=False)

    return service, mock_journal


# ─────────────────────────────────────────────────────────────────────────────
# Test 1: _activity_journal.init() is called during refinement cycle
# ─────────────────────────────────────────────────────────────────────────────


class TestJournalInitIsCalled:
    """Test 1: _activity_journal.init() must be called when lock is acquired."""

    def test_journal_init_called_on_normal_execution(self, tmp_path):
        """init() must be called with a Path object."""
        domains = [{"name": "domain-A"}, {"name": "domain-B"}]
        service, mock_journal = _make_service(tmp_path, domains)

        service.run_refinement_cycle()

        mock_journal.init.assert_called_once()

    def test_journal_init_receives_a_path(self, tmp_path):
        """init() must be called with a pathlib.Path argument."""
        domains = [{"name": "domain-A"}]
        service, mock_journal = _make_service(tmp_path, domains)

        service.run_refinement_cycle()

        mock_journal.init.assert_called_once()
        init_arg = mock_journal.init.call_args[0][0]
        assert isinstance(
            init_arg, Path
        ), f"init() should receive a Path, got {type(init_arg)}"

    def test_journal_logs_disabled_when_refinement_disabled(self, tmp_path):
        """init() IS called when refinement_enabled=False to log the skip reason."""
        domains = [{"name": "domain-A"}]
        service, mock_journal = _make_service(
            tmp_path, domains, refinement_enabled=False
        )

        service.run_refinement_cycle()

        mock_journal.init.assert_called_once()
        # Should log a "disabled" message
        disabled_calls = [
            c
            for c in mock_journal.log.call_args_list
            if c.args and "disabled" in str(c.args[0]).lower()
        ]
        assert len(disabled_calls) >= 1, (
            "A 'disabled in config' journal entry should be logged. "
            f"Actual calls: {[str(c) for c in mock_journal.log.call_args_list]}"
        )

    def test_journal_logs_skipped_when_lock_fails(self, tmp_path):
        """init() IS called when lock fails to log the skip reason."""
        domains = [{"name": "domain-A"}]
        service, mock_journal = _make_service(tmp_path, domains)
        service._lock.acquire.return_value = False

        service.run_refinement_cycle()

        mock_journal.init.assert_called_once()
        skipped_calls = [
            c
            for c in mock_journal.log.call_args_list
            if c.args and "skipped" in str(c.args[0]).lower()
        ]
        assert len(skipped_calls) >= 1, (
            "A 'skipped - analysis already in progress' journal entry should be logged. "
            f"Actual calls: {[str(c) for c in mock_journal.log.call_args_list]}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Test 2: "Starting refinement cycle" log message is emitted
# ─────────────────────────────────────────────────────────────────────────────


class TestStartingRefinementCycleMessage:
    """Test 2: "Starting refinement cycle" must be logged after lock acquisition."""

    def test_starting_message_logged(self, tmp_path):
        """log("Starting refinement cycle") must be called."""
        domains = [{"name": "domain-A"}]
        service, mock_journal = _make_service(tmp_path, domains)

        service.run_refinement_cycle()

        log_calls = [str(c) for c in mock_journal.log.call_args_list]
        starting_calls = [
            c
            for c in mock_journal.log.call_args_list
            if "Starting refinement cycle" in str(c.args)
        ]
        assert len(starting_calls) >= 1, (
            "log('Starting refinement cycle') must be called. "
            f"Actual calls: {log_calls}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Test 3: Per-domain log messages are emitted
# ─────────────────────────────────────────────────────────────────────────────


class TestPerDomainLogMessages:
    """Test 3: Each domain in batch should have a before and after log message."""

    def test_domain_name_appears_in_log_calls(self, tmp_path):
        """Each domain name must appear in at least one log call."""
        domains = [{"name": "payments"}, {"name": "auth"}]
        service, mock_journal = _make_service(tmp_path, domains, domains_per_run=2)

        service.run_refinement_cycle()

        all_log_messages = " ".join(
            str(c.args[0]) for c in mock_journal.log.call_args_list if c.args
        )
        assert (
            "payments" in all_log_messages
        ), "Domain 'payments' must appear in journal log messages"
        assert (
            "auth" in all_log_messages
        ), "Domain 'auth' must appear in journal log messages"

    def test_before_domain_message_logged(self, tmp_path):
        """A 'Refining domain' message should be logged before each domain."""
        domains = [{"name": "billing"}]
        service, mock_journal = _make_service(tmp_path, domains, domains_per_run=1)

        service.run_refinement_cycle()

        refining_calls = [
            c
            for c in mock_journal.log.call_args_list
            if c.args
            and "Refining domain" in str(c.args[0])
            and "billing" in str(c.args[0])
        ]
        assert len(refining_calls) >= 1, (
            "A 'Refining domain' message for 'billing' must be logged. "
            f"Actual calls: {[str(c) for c in mock_journal.log.call_args_list]}"
        )

    def test_after_domain_success_message_logged_no_change(self, tmp_path):
        """After domain refinement with no change, a success message should be logged."""
        domains = [{"name": "orders"}]
        service, mock_journal = _make_service(tmp_path, domains, domains_per_run=1)
        service.refine_or_create_domain.return_value = False  # no changes

        service.run_refinement_cycle()

        success_calls = [
            c
            for c in mock_journal.log.call_args_list
            if c.args
            and "orders" in str(c.args[0])
            and (
                "refined" in str(c.args[0]).lower()
                or "no changes" in str(c.args[0]).lower()
            )
        ]
        assert len(success_calls) >= 1, (
            "A completion message for 'orders' must be logged after refinement. "
            f"Actual calls: {[str(c) for c in mock_journal.log.call_args_list]}"
        )

    def test_after_domain_success_message_logged_with_change(self, tmp_path):
        """After domain refinement with change, a changed success message should be logged."""
        domains = [{"name": "inventory"}]
        service, mock_journal = _make_service(tmp_path, domains, domains_per_run=1)
        service.refine_or_create_domain.return_value = True  # changed

        service.run_refinement_cycle()

        success_calls = [
            c
            for c in mock_journal.log.call_args_list
            if c.args
            and "inventory" in str(c.args[0])
            and (
                "refined" in str(c.args[0]).lower()
                or "changed" in str(c.args[0]).lower()
            )
        ]
        assert len(success_calls) >= 1, (
            "A changed success message for 'inventory' must be logged. "
            f"Actual calls: {[str(c) for c in mock_journal.log.call_args_list]}"
        )

    def test_domain_failure_message_logged(self, tmp_path):
        """When domain refinement fails, a failure message must be logged."""
        domains = [{"name": "reporting"}]
        service, mock_journal = _make_service(tmp_path, domains, domains_per_run=1)
        service.refine_or_create_domain.side_effect = RuntimeError("Claude timeout")

        service.run_refinement_cycle()

        failure_calls = [
            c
            for c in mock_journal.log.call_args_list
            if c.args
            and "reporting" in str(c.args[0])
            and (
                "failed" in str(c.args[0]).lower() or "error" in str(c.args[0]).lower()
            )
        ]
        assert len(failure_calls) >= 1, (
            "A failure message for 'reporting' must be logged on exception. "
            f"Actual calls: {[str(c) for c in mock_journal.log.call_args_list]}"
        )

    def test_batch_selection_message_logged(self, tmp_path):
        """A batch selection message with count should be logged."""
        domains = [{"name": "domain-1"}, {"name": "domain-2"}, {"name": "domain-3"}]
        service, mock_journal = _make_service(tmp_path, domains, domains_per_run=2)

        service.run_refinement_cycle()

        batch_calls = [
            c
            for c in mock_journal.log.call_args_list
            if c.args
            and (
                "batch" in str(c.args[0]).lower()
                or "processing" in str(c.args[0]).lower()
            )
        ]
        assert len(batch_calls) >= 1, (
            "A batch selection message must be logged. "
            f"Actual calls: {[str(c) for c in mock_journal.log.call_args_list]}"
        )


# ─────────────────────────────────────────────────────────────────────────────
# Test 4: Completion log message is emitted
# ─────────────────────────────────────────────────────────────────────────────


class TestCompletionLogMessage:
    """Test 4: A completion message must be logged at end of cycle."""

    def test_completion_message_contains_cycle_info(self, tmp_path):
        """A 'Refinement cycle complete' (or similar) message must be logged."""
        domains = [{"name": "checkout"}, {"name": "catalog"}]
        service, mock_journal = _make_service(tmp_path, domains, domains_per_run=2)

        service.run_refinement_cycle()

        completion_calls = [
            c
            for c in mock_journal.log.call_args_list
            if c.args
            and (
                "complete" in str(c.args[0]).lower()
                or "refinement cycle" in str(c.args[0]).lower()
            )
        ]
        assert len(completion_calls) >= 1, (
            "A completion message must be logged at end of cycle. "
            f"Actual calls: {[str(c) for c in mock_journal.log.call_args_list]}"
        )

    def test_completion_message_includes_domain_count(self, tmp_path):
        """The completion message must include processed domain count."""
        domains = [{"name": "a"}, {"name": "b"}]
        service, mock_journal = _make_service(tmp_path, domains, domains_per_run=2)

        service.run_refinement_cycle()

        # Find any log message containing digits (domain count)
        completion_calls = [
            c
            for c in mock_journal.log.call_args_list
            if c.args
            and (
                "complete" in str(c.args[0]).lower()
                or "processed" in str(c.args[0]).lower()
            )
        ]
        assert len(completion_calls) >= 1, (
            "A completion message with domain count must be logged. "
            f"Actual calls: {[str(c) for c in mock_journal.log.call_args_list]}"
        )
        # Check that the message contains a number
        completion_msg = str(completion_calls[0].args[0])
        import re

        has_number = bool(re.search(r"\d", completion_msg))
        assert (
            has_number
        ), f"Completion message should include a count, got: '{completion_msg}'"


# ─────────────────────────────────────────────────────────────────────────────
# Test 5: Journal errors do not break refinement (non-fatal)
# ─────────────────────────────────────────────────────────────────────────────


class TestJournalErrorsNonFatal:
    """Test 5: Journal exceptions must not propagate (wrapped in try/except)."""

    def test_journal_init_error_does_not_break_refinement(self, tmp_path):
        """If journal.init() raises, refinement still completes normally."""
        domains = [{"name": "domain-A"}]
        service, mock_journal = _make_service(tmp_path, domains, domains_per_run=1)
        mock_journal.init.side_effect = RuntimeError("Journal init failed")

        # Should NOT raise
        service.run_refinement_cycle()

        # refine_or_create_domain should still be called
        service.refine_or_create_domain.assert_called_once()

    def test_journal_log_error_does_not_break_refinement(self, tmp_path):
        """If journal.log() raises, refinement still completes normally."""
        domains = [{"name": "domain-A"}, {"name": "domain-B"}]
        service, mock_journal = _make_service(tmp_path, domains, domains_per_run=2)
        mock_journal.log.side_effect = OSError("Disk full")

        # Should NOT raise
        service.run_refinement_cycle()

        # Both domains should still be processed
        assert (
            service.refine_or_create_domain.call_count == 2
        ), "Both domains must be processed even when journal.log() raises"

    def test_journal_log_error_mid_loop_does_not_skip_domains(self, tmp_path):
        """If journal.log() raises during loop, remaining domains still process."""
        domains = [{"name": "first"}, {"name": "second"}, {"name": "third"}]
        service, mock_journal = _make_service(tmp_path, domains, domains_per_run=3)

        call_count = [0]

        def flaky_log(msg):
            call_count[0] += 1
            if call_count[0] % 2 == 0:
                raise IOError("Intermittent journal error")

        mock_journal.log.side_effect = flaky_log

        # Should NOT raise
        service.run_refinement_cycle()

        # All 3 domains should be processed
        assert (
            service.refine_or_create_domain.call_count == 3
        ), "All 3 domains must be processed despite intermittent journal errors"

    def test_tracking_update_still_called_when_journal_fails(self, tmp_path):
        """Cursor update (tracking_backend.update_tracking) runs even if journal fails."""
        domains = [{"name": "domain-A"}]
        service, mock_journal = _make_service(tmp_path, domains)
        mock_journal.log.side_effect = RuntimeError("Journal broken")

        service.run_refinement_cycle()

        service._tracking_backend.update_tracking.assert_called()


# ─────────────────────────────────────────────────────────────────────────────
# Test 6: Journal init uses the correct directory path
# ─────────────────────────────────────────────────────────────────────────────


class TestJournalInitDirectoryPath:
    """Test 6: init() must use ~/.tmp/depmap-refinement-journal/ path."""

    def test_journal_init_uses_refinement_journal_dir(self, tmp_path):
        """init() must be called with a path containing 'depmap-refinement-journal'."""
        domains = [{"name": "domain-A"}]
        service, mock_journal = _make_service(tmp_path, domains)

        service.run_refinement_cycle()

        mock_journal.init.assert_called_once()
        init_path = mock_journal.init.call_args[0][0]
        assert "depmap-refinement-journal" in str(
            init_path
        ), f"init() path must contain 'depmap-refinement-journal', got: {init_path}"

    def test_journal_init_path_is_under_home_tmp(self, tmp_path):
        """init() path must be under ~/.tmp/."""
        domains = [{"name": "domain-A"}]
        service, mock_journal = _make_service(tmp_path, domains)

        service.run_refinement_cycle()

        init_path = mock_journal.init.call_args[0][0]
        expected_prefix = os.path.expanduser("~/.tmp")
        assert str(
            init_path
        ).startswith(
            expected_prefix
        ), f"init() path must start with '~/.tmp' (~={expected_prefix}), got: {init_path}"

    def test_journal_init_path_matches_exact_pattern(self, tmp_path):
        """init() path must exactly match ~/.tmp/depmap-refinement-journal/."""
        domains = [{"name": "domain-A"}]
        service, mock_journal = _make_service(tmp_path, domains)

        service.run_refinement_cycle()

        init_path = mock_journal.init.call_args[0][0]
        expected = Path(os.path.expanduser("~/.tmp/depmap-refinement-journal/"))
        assert init_path == expected, f"Expected exact path {expected}, got {init_path}"
