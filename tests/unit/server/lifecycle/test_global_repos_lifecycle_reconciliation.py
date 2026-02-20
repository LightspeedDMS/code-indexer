"""
Unit tests for GlobalReposLifecycleManager reconciliation wiring (Story #236).

Tests that server startup triggers reconcile_golden_repos() on the RefreshScheduler
after global repos background services are started.

The reconciliation must run in a background thread (non-blocking) so it doesn't
delay server startup. Failures must not block startup (AC7).
"""

import time
import pytest
from unittest.mock import patch

from code_indexer.server.lifecycle.global_repos_lifecycle import GlobalReposLifecycleManager


@pytest.fixture
def golden_repos_dir(tmp_path):
    """Create a temporary golden-repos directory."""
    golden_dir = tmp_path / "golden-repos"
    golden_dir.mkdir(parents=True)
    return golden_dir


class TestGlobalReposLifecycleReconciliation:
    """
    Tests that GlobalReposLifecycleManager wires reconcile_golden_repos()
    into the startup sequence (Story #236 wiring requirement).
    """

    def test_start_triggers_reconciliation_in_background(self, golden_repos_dir):
        """
        Story #236: reconcile_golden_repos() must be called on the RefreshScheduler
        during startup, running in a background thread (non-blocking).
        """
        manager = GlobalReposLifecycleManager(str(golden_repos_dir))

        reconcile_called = []

        def capture_reconcile(*args, **kwargs):
            reconcile_called.append(True)

        with patch.object(
            manager.refresh_scheduler,
            "reconcile_golden_repos",
            side_effect=capture_reconcile,
        ):
            manager.start()

            # Give the background thread time to invoke reconcile_golden_repos
            deadline = time.time() + 2.0
            while not reconcile_called and time.time() < deadline:
                time.sleep(0.05)

            manager.stop()

        assert len(reconcile_called) >= 1, (
            "reconcile_golden_repos() must be called during startup"
        )

    def test_reconcile_failure_does_not_block_start(self, golden_repos_dir):
        """
        AC7: If reconcile_golden_repos raises on the RefreshScheduler,
        startup must still complete normally and manager must be running.
        """
        manager = GlobalReposLifecycleManager(str(golden_repos_dir))

        with patch.object(
            manager.refresh_scheduler,
            "reconcile_golden_repos",
            side_effect=RuntimeError("reconciliation failed"),
        ):
            # Must not raise
            manager.start()

            # Give background thread time to run (and fail)
            time.sleep(0.1)

            # Manager must be running despite reconciliation failure
            assert manager.is_running(), (
                "Lifecycle manager must be running even after reconciliation failure"
            )

            manager.stop()
