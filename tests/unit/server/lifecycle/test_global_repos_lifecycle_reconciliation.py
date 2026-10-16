"""
Unit tests for GlobalReposLifecycleManager reconciliation wiring (Story #236).

Tests that server startup triggers reconcile_golden_repos() on the RefreshScheduler
after global repos background services are started.

The reconciliation must run in a background thread (non-blocking) so it doesn't
delay server startup. Failures must not block startup (AC7).
"""

import logging
import threading
import time

import pytest
from unittest.mock import patch

from code_indexer.server.lifecycle.global_repos_lifecycle import (
    GlobalReposLifecycleManager,
)
from tests.fixtures.refresh_scheduler_stores import (
    initialize_server_database,
    scheduler_iteration_failures,
)


@pytest.fixture
def golden_repos_dir(tmp_path):
    """Create a temporary golden-repos directory.  The manager's scheduler
    resolves its registry and metadata stores from golden_repos_dir.parent,
    so that server database is initialized first, as the server does at
    startup (without it every loop iteration fails: no such table)."""
    golden_dir = tmp_path / "golden-repos"
    golden_dir.mkdir(parents=True)
    initialize_server_database(tmp_path)
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

    def test_reconcile_failure_does_not_block_start(self, golden_repos_dir, caplog):
        """
        AC7: If reconcile_golden_repos raises on the RefreshScheduler,
        startup must still complete normally, the manager must be running,
        and the scheduler loop must run its iterations without error.
        """
        manager = GlobalReposLifecycleManager(str(golden_repos_dir))
        registry = manager.refresh_scheduler.registry
        real_due = registry.list_due_repos
        due_query_reached = threading.Event()

        def signal_due_query(*args, **kwargs):
            try:
                return real_due(*args, **kwargs)
            finally:
                due_query_reached.set()

        with (
            caplog.at_level(logging.ERROR),
            patch.object(
                manager.refresh_scheduler,
                "reconcile_golden_repos",
                side_effect=RuntimeError("reconciliation failed"),
            ),
            patch.object(registry, "list_due_repos", side_effect=signal_due_query),
        ):
            # Must not raise
            manager.start()
            try:
                # Manager must be running despite reconciliation failure
                assert manager.is_running(), (
                    "Lifecycle manager must be running even after reconciliation failure"
                )
                assert due_query_reached.wait(timeout=5), (
                    "scheduler loop never reached its due-repo query"
                )
            finally:
                manager.stop()

        assert scheduler_iteration_failures(caplog.records) == []
