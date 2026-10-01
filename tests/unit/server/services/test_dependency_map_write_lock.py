"""
Unit tests for write lock acquire/release behavior in DependencyMapService.

Verifies that:
1. When acquire_write_lock returns True, release_write_lock IS called in finally
2. When acquire_write_lock returns False, release_write_lock is NOT called
3. run_full_analysis(), run_delta_analysis(), AND run_refinement_cycle() are covered
4. Bug #1506 4th-pass review Item 2: when acquire_write_lock returns False,
   the analysis is now SKIPPED entirely -- real analysis work never
   proceeds despite a failed acquisition. Previously the acquired-flag
   was recorded but never checked, so DependencyMapService proceeded to
   mutate cidx-meta source files even while RefreshScheduler's own
   _held_write_lock_for_publish() held the SAME per-repo lock across its
   index -> integrity-gate -> snapshot -> swap-alias publish sequence,
   defeating the mutual exclusion the lock exists to provide.

Story #227: write-lock coordination with RefreshScheduler.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.services.dependency_map_service import DependencyMapService


def _make_service(refresh_scheduler=None):
    """Create a DependencyMapService with minimal mocked dependencies."""
    golden_repos_manager = MagicMock()
    golden_repos_manager.golden_repos_dir = "/tmp/golden-repos"

    config_manager = MagicMock()
    tracking_backend = MagicMock()
    analyzer = MagicMock()

    service = DependencyMapService(
        golden_repos_manager=golden_repos_manager,
        config_manager=config_manager,
        tracking_backend=tracking_backend,
        analyzer=analyzer,
        refresh_scheduler=refresh_scheduler,
    )
    return service, config_manager, tracking_backend, golden_repos_manager


def _make_disabled_config():
    """Return a config mock where dependency_map_enabled is False."""
    config = MagicMock()
    config.dependency_map_enabled = False
    return config


class TestFullAnalysisWriteLock:
    """Tests for write lock behavior in run_full_analysis()."""

    def test_releases_lock_when_acquire_returns_true(self):
        """When acquire_write_lock returns True, release_write_lock must be called."""
        refresh_scheduler = MagicMock()
        refresh_scheduler.acquire_write_lock.return_value = True

        service, config_manager, tracking_backend, _ = _make_service(refresh_scheduler)

        # Make _setup_analysis return early (disabled) to avoid complex mock setup
        config_manager.get_claude_integration_config.return_value = (
            _make_disabled_config()
        )

        service.run_full_analysis()

        refresh_scheduler.acquire_write_lock.assert_called_once_with(
            "cidx-meta", owner_name="dependency_map_service"
        )
        refresh_scheduler.release_write_lock.assert_called_once_with(
            "cidx-meta", owner_name="dependency_map_service"
        )

    def test_does_not_release_lock_when_acquire_returns_false(self):
        """When acquire_write_lock returns False, release_write_lock must NOT be called."""
        refresh_scheduler = MagicMock()
        refresh_scheduler.acquire_write_lock.return_value = False

        service, config_manager, tracking_backend, _ = _make_service(refresh_scheduler)

        # Make _setup_analysis return early (disabled) to avoid complex mock setup
        config_manager.get_claude_integration_config.return_value = (
            _make_disabled_config()
        )

        service.run_full_analysis()

        refresh_scheduler.acquire_write_lock.assert_called_once_with(
            "cidx-meta", owner_name="dependency_map_service"
        )
        refresh_scheduler.release_write_lock.assert_not_called()

    def test_releases_lock_on_exception_when_acquire_returned_true(self):
        """release_write_lock is still called in finally even when an exception occurs."""
        refresh_scheduler = MagicMock()
        refresh_scheduler.acquire_write_lock.return_value = True

        service, config_manager, tracking_backend, _ = _make_service(refresh_scheduler)

        # Make _setup_analysis raise to simulate an analysis failure
        config_manager.get_claude_integration_config.side_effect = RuntimeError(
            "config error"
        )

        with pytest.raises(RuntimeError, match="config error"):
            service.run_full_analysis()

        # Lock must have been acquired and then released in finally
        refresh_scheduler.acquire_write_lock.assert_called_once()
        refresh_scheduler.release_write_lock.assert_called_once_with(
            "cidx-meta", owner_name="dependency_map_service"
        )

    def test_does_not_release_lock_and_skips_analysis_when_acquire_returns_false(self):
        """Bug #1506 4th-pass review Item 2: when acquire_write_lock returns
        False, run_full_analysis() must skip entirely -- it must never call
        _setup_analysis() (and therefore never call
        get_claude_integration_config()) and must never let an unrelated
        exception from that skipped work propagate. Previously this test
        (named test_does_not_release_lock_on_exception_when_acquire_
        returned_false) pinned the OPPOSITE (buggy) behavior: it asserted a
        RuntimeError raised by the config lookup still propagated even
        though the lock was never acquired, proving the analysis proceeded
        regardless of the failed acquisition."""
        refresh_scheduler = MagicMock()
        refresh_scheduler.acquire_write_lock.return_value = False

        service, config_manager, tracking_backend, _ = _make_service(refresh_scheduler)

        config_manager.get_claude_integration_config.side_effect = RuntimeError(
            "config error"
        )

        result = service.run_full_analysis()

        refresh_scheduler.acquire_write_lock.assert_called_once()
        refresh_scheduler.release_write_lock.assert_not_called()
        config_manager.get_claude_integration_config.assert_not_called()
        assert result is not None and result.get("status") == "skipped"

    def test_no_acquire_or_release_when_refresh_scheduler_is_none(self):
        """When no refresh_scheduler, neither acquire nor release is attempted."""
        service, config_manager, _, _ = _make_service(refresh_scheduler=None)
        config_manager.get_claude_integration_config.return_value = (
            _make_disabled_config()
        )

        # Should not raise and should not call any scheduler method
        service.run_full_analysis()
        # No assertions needed - the fact that it completes without AttributeError confirms it


class TestDeltaAnalysisWriteLock:
    """Tests for write lock behavior in run_delta_analysis()."""

    def _make_delta_config(self):
        """Return a config mock where dependency_map_enabled is False to short-circuit delta."""
        config = MagicMock()
        config.dependency_map_enabled = False
        return config

    def test_releases_lock_when_acquire_returns_true(self):
        """When acquire_write_lock returns True, release_write_lock must be called."""
        refresh_scheduler = MagicMock()
        refresh_scheduler.acquire_write_lock.return_value = True

        service, config_manager, tracking_backend, _ = _make_service(refresh_scheduler)

        # dependency_map_enabled=False causes early return inside try block
        config_manager.get_claude_integration_config.return_value = (
            self._make_delta_config()
        )

        service.run_delta_analysis()

        refresh_scheduler.acquire_write_lock.assert_called_once_with(
            "cidx-meta", owner_name="dependency_map_service"
        )
        refresh_scheduler.release_write_lock.assert_called_once_with(
            "cidx-meta", owner_name="dependency_map_service"
        )

    def test_does_not_release_lock_when_acquire_returns_false(self):
        """When acquire_write_lock returns False, release_write_lock must NOT be called."""
        refresh_scheduler = MagicMock()
        refresh_scheduler.acquire_write_lock.return_value = False

        service, config_manager, tracking_backend, _ = _make_service(refresh_scheduler)

        # dependency_map_enabled=False causes early return inside try block
        config_manager.get_claude_integration_config.return_value = (
            self._make_delta_config()
        )

        service.run_delta_analysis()

        refresh_scheduler.acquire_write_lock.assert_called_once_with(
            "cidx-meta", owner_name="dependency_map_service"
        )
        refresh_scheduler.release_write_lock.assert_not_called()

    def test_releases_lock_on_exception_when_acquire_returned_true(self):
        """release_write_lock is still called in finally even when exception occurs."""
        refresh_scheduler = MagicMock()
        refresh_scheduler.acquire_write_lock.return_value = True

        service, config_manager, tracking_backend, _ = _make_service(refresh_scheduler)

        config_manager.get_claude_integration_config.side_effect = RuntimeError(
            "config error"
        )

        with pytest.raises(RuntimeError, match="config error"):
            service.run_delta_analysis()

        refresh_scheduler.acquire_write_lock.assert_called_once()
        refresh_scheduler.release_write_lock.assert_called_once_with(
            "cidx-meta", owner_name="dependency_map_service"
        )

    def test_does_not_release_lock_and_skips_analysis_when_acquire_returns_false(self):
        """Bug #1506 4th-pass review Item 2 (delta variant): when
        acquire_write_lock returns False, run_delta_analysis() must skip
        entirely -- it must never call get_claude_integration_config(),
        and no unrelated exception from that skipped work can propagate.
        Previously this test (named test_does_not_release_lock_on_
        exception_when_acquire_returned_false) pinned the opposite
        (buggy) behavior."""
        refresh_scheduler = MagicMock()
        refresh_scheduler.acquire_write_lock.return_value = False

        service, config_manager, tracking_backend, _ = _make_service(refresh_scheduler)

        config_manager.get_claude_integration_config.side_effect = RuntimeError(
            "config error"
        )

        result = service.run_delta_analysis()

        refresh_scheduler.acquire_write_lock.assert_called_once()
        refresh_scheduler.release_write_lock.assert_not_called()
        config_manager.get_claude_integration_config.assert_not_called()
        assert result is None

    def test_no_acquire_or_release_when_refresh_scheduler_is_none(self):
        """When no refresh_scheduler, neither acquire nor release is attempted."""
        service, config_manager, _, _ = _make_service(refresh_scheduler=None)
        config = MagicMock()
        config.dependency_map_enabled = False
        config_manager.get_claude_integration_config.return_value = config

        # Should complete without AttributeError
        service.run_delta_analysis()


class TestRefinementCycleWriteLock:
    """Bug #1506 4th-pass review Item 2 (refinement variant):
    run_refinement_cycle() acquires the same 'cidx-meta' write lock
    (Story #227) but -- prior to this fix -- proceeded with real domain
    refinement work regardless of whether the acquisition succeeded. This
    class proves the fixed skip-on-failed-acquire behavior."""

    def test_skips_when_acquire_returns_false(self, tmp_path):
        import json

        refresh_scheduler = MagicMock()
        refresh_scheduler.acquire_write_lock.return_value = False

        service, config_manager, tracking_backend, golden_repos_manager = _make_service(
            refresh_scheduler
        )
        # Use a real tmp_path so ActivityJournalService.init() (called
        # before the skip check) has a genuine writable directory rather
        # than the module-default "/tmp/golden-repos" string.
        golden_repos_manager.golden_repos_dir = str(tmp_path)

        # A discriminating RED/GREEN setup requires a REAL, non-empty
        # _domains.json under the versioned read path -- otherwise the
        # "domains_json_path not found, skipping" branch short-circuits
        # BEFORE reaching tracking_backend.get_tracking() regardless of
        # whether the write-lock skip check fired, making the test pass
        # even against the unfixed, buggy production code.
        versioned_dep_map = (
            Path(tmp_path) / ".versioned" / "cidx-meta" / "v_test" / "dependency-map"
        )
        versioned_dep_map.mkdir(parents=True)
        (versioned_dep_map / "_domains.json").write_text(
            json.dumps(
                [{"name": "auth-domain", "participating_repos": ["auth-service"]}]
            )
        )

        config = MagicMock()
        config.refinement_enabled = True
        config.refinement_domains_per_run = 1
        config.refinement_interval_hours = 24
        config_manager.get_claude_integration_config.return_value = config
        # If the skip check does NOT fire (unfixed behavior), real work
        # must be able to complete cleanly through _select_domain_batch
        # instead of crashing on unconfigured MagicMock arithmetic -- the
        # test's own assertion below must be what fails, not an unrelated
        # IndexError/TypeError.
        tracking_backend.get_tracking.return_value = {"refinement_cursor": 0}

        result = service.run_refinement_cycle()

        assert result is None
        refresh_scheduler.acquire_write_lock.assert_called_once_with(
            "cidx-meta", owner_name="dependency_map_service"
        )
        refresh_scheduler.release_write_lock.assert_not_called()
        # The real domain-cycling work (selecting a batch, advancing the
        # cursor via tracking_backend) must never have started.
        tracking_backend.get_tracking.assert_not_called()

    def test_proceeds_and_releases_when_acquire_returns_true(self, tmp_path):
        """Regression guard: a successful acquire must still reach the
        real domain-cycling work and release the lock afterward -- the
        fix must not accidentally skip the healthy path too."""
        refresh_scheduler = MagicMock()
        refresh_scheduler.acquire_write_lock.return_value = True

        service, config_manager, tracking_backend, golden_repos_manager = _make_service(
            refresh_scheduler
        )
        golden_repos_manager.golden_repos_dir = str(tmp_path)

        config = MagicMock()
        config.refinement_enabled = True
        config_manager.get_claude_integration_config.return_value = config
        # No _domains.json on disk -> real work reaches the "not found,
        # skipping cycle" branch quickly, but tracking_backend is never
        # consulted on THAT path either -- so assert on the lock instead.

        result = service.run_refinement_cycle()

        assert result is None
        refresh_scheduler.acquire_write_lock.assert_called_once_with(
            "cidx-meta", owner_name="dependency_map_service"
        )
        refresh_scheduler.release_write_lock.assert_called_once_with(
            "cidx-meta", owner_name="dependency_map_service"
        )


def _make_service_with_lifecycle(refresh_scheduler, golden_repos_dir):
    """Create a DependencyMapService with job_tracker/lifecycle_invoker/
    lifecycle_debouncer all wired (non-None), so the lifecycle fleet
    pre-flight condition in run_full_analysis()/run_delta_analysis() is
    satisfied and would run if not correctly gated by the write lock.
    golden_repos_dir is caller-injected (e.g. a pytest tmp_path) rather
    than hardcoded, so no test depends on a real filesystem location."""
    golden_repos_manager = MagicMock()
    golden_repos_manager.golden_repos_dir = str(golden_repos_dir)
    golden_repos_manager.list_golden_repos.return_value = [{"alias": "repo1"}]

    config_manager = MagicMock()
    tracking_backend = MagicMock()
    analyzer = MagicMock()
    job_tracker = MagicMock()
    lifecycle_invoker = MagicMock()
    lifecycle_debouncer = MagicMock()

    service = DependencyMapService(
        golden_repos_manager=golden_repos_manager,
        config_manager=config_manager,
        tracking_backend=tracking_backend,
        analyzer=analyzer,
        refresh_scheduler=refresh_scheduler,
        job_tracker=job_tracker,
        lifecycle_invoker=lifecycle_invoker,
        lifecycle_debouncer=lifecycle_debouncer,
    )
    return service, golden_repos_manager, job_tracker


@pytest.mark.parametrize("method_name", ["run_full_analysis", "run_delta_analysis"])
class TestLifecyclePreflightSkip:
    """Bug #1506 5th-pass review Item 1: the lifecycle fleet pre-flight
    (LifecycleFleetScanner/LifecycleBatchRunner -- a real Claude CLI call
    per broken repo, repairing cidx-meta/<alias>.md files) in BOTH
    run_full_analysis() and run_delta_analysis() must never run when the
    write lock cannot be acquired. The 4th-pass fix only skipped the MAIN
    analysis after the pre-flight had already run unprotected; this
    proves the pre-flight itself is now gated too, and that the write
    lock is still released if the pre-flight itself raises."""

    def test_lifecycle_preflight_never_constructed_when_write_lock_fails(
        self, method_name, tmp_path
    ):
        refresh_scheduler = MagicMock()
        refresh_scheduler.acquire_write_lock.return_value = False

        service, golden_repos_manager, job_tracker = _make_service_with_lifecycle(
            refresh_scheduler, tmp_path
        )

        with (
            patch(
                "code_indexer.server.services.dependency_map_service.LifecycleFleetScanner"
            ) as mock_scanner_cls,
            patch(
                "code_indexer.server.services.dependency_map_service.LifecycleBatchRunner"
            ) as mock_runner_cls,
        ):
            result = getattr(service, method_name)()

        mock_scanner_cls.assert_not_called()
        mock_runner_cls.assert_not_called()
        golden_repos_manager.list_golden_repos.assert_not_called()
        if method_name == "run_full_analysis":
            assert result is not None and result.get("status") == "skipped"
        else:
            assert result is None

    def test_lifecycle_preflight_exception_still_releases_write_lock(
        self, method_name, tmp_path
    ):
        """Exception-safety guard: if the lifecycle pre-flight itself
        raises (e.g. LifecycleBatchRunner.run() fails), the already-
        acquired write lock must still be released before the exception
        propagates -- it must not leak."""
        refresh_scheduler = MagicMock()
        refresh_scheduler.acquire_write_lock.return_value = True

        service, golden_repos_manager, job_tracker = _make_service_with_lifecycle(
            refresh_scheduler, tmp_path
        )
        # Trigger the lifecycle preflight's broken-repo path so
        # LifecycleBatchRunner.run() is reached and can raise.
        with (
            patch(
                "code_indexer.server.services.dependency_map_service.LifecycleFleetScanner"
            ) as mock_scanner_cls,
            patch(
                "code_indexer.server.services.dependency_map_service.LifecycleBatchRunner"
            ) as mock_runner_cls,
        ):
            mock_scanner_cls.return_value.find_broken_or_missing.return_value = [
                "repo1"
            ]
            mock_runner_cls.return_value.run.side_effect = RuntimeError(
                "lifecycle repair failed"
            )

            with pytest.raises(RuntimeError, match="lifecycle repair failed"):
                getattr(service, method_name)()

        refresh_scheduler.release_write_lock.assert_called_once_with(
            "cidx-meta", owner_name="dependency_map_service"
        )


class TestRefinementCycleConfigReadPreflightSkip:
    """Bug #1506 5th-pass review Item 1 (refinement variant):
    run_refinement_cycle()'s config read (and disabled-check / in-process
    lock / activity-journal writes) must never run when the write lock
    cannot be acquired -- previously the config read happened first,
    regardless of the write lock outcome."""

    def test_config_read_never_happens_when_write_lock_fails(self):
        refresh_scheduler = MagicMock()
        refresh_scheduler.acquire_write_lock.return_value = False

        service, config_manager, tracking_backend, _ = _make_service(refresh_scheduler)

        result = service.run_refinement_cycle()

        assert result is None
        config_manager.get_claude_integration_config.assert_not_called()
        refresh_scheduler.release_write_lock.assert_not_called()
