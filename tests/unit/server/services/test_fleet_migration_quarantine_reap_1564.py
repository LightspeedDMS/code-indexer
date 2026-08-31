"""Unit tests for Bug #1564 (part 1/2): fleet-migration quarantine rows
for a golden repo that no longer exists are reaped by
`quarantine.reconcile_stale_quarantine_rows()`, and that function fails
open (never raises) when the backend or golden-repo list is unavailable.

See test_fleet_migration_quarantine_health_evidence_1564.py for the
positive-evidence auto-clear / genuinely-broken-stays-reported /
disk-headroom-not-duplicated coverage of the same function.
"""

import os
import tempfile
from pathlib import Path
from typing import Dict, List

import pytest

from code_indexer.server.services.fleet_migration.quarantine import (
    GENERIC_FAILURE_CAUSE,
    get_failure_state,
    record_migration_failure,
    record_unrecoverable_corruption,
    reconcile_stale_quarantine_rows,
)
from code_indexer.server.storage.sqlite_backends import GoldenRepoMetadataSqliteBackend


class _FakeGoldenRepoManager:
    """Minimal test double: real `_sqlite_backend` (mirrors
    `GoldenRepoManager`'s own attribute name), plus `list_golden_repos()`
    and `get_actual_repo_path()` -- the exact surface
    `discovery.enumerate_fleet_migration_candidates()` requires."""

    def __init__(self, sqlite_backend, repos: Dict[str, Path]):
        self._sqlite_backend = sqlite_backend
        self._repos = repos

    def list_golden_repos(self) -> List[dict]:
        return [{"alias": alias} for alias in self._repos]

    def get_actual_repo_path(self, alias: str) -> str:
        return str(self._repos[alias])


@pytest.fixture
def backend():
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = os.path.join(temp_dir, "test.db")
        be = GoldenRepoMetadataSqliteBackend(db_path)
        be.ensure_table_exists()
        try:
            yield be
        finally:
            be.close()


class TestReapsOrphanedAliasRows:
    def test_row_for_alias_no_longer_registered_is_deleted(self, backend):
        manager = _FakeGoldenRepoManager(backend, repos={})
        record_unrecoverable_corruption(manager, "evolution", "corrupt detail")

        reconcile_stale_quarantine_rows(manager)

        assert get_failure_state(manager, "evolution") is None

    def test_below_threshold_row_for_orphaned_alias_is_still_reaped(self, backend):
        """A dangling row is garbage regardless of its failure count --
        reaping-by-alias must not be gated on the quarantine threshold."""
        manager = _FakeGoldenRepoManager(backend, repos={})
        record_migration_failure(
            manager, "evolution", "sig", failure_cause=GENERIC_FAILURE_CAUSE
        )

        reconcile_stale_quarantine_rows(manager)

        assert get_failure_state(manager, "evolution") is None


class TestNeverRaisesOnBackendFailures:
    def test_missing_backend_is_a_silent_no_op(self):
        class _NoBackendManager:
            def list_golden_repos(self):
                return []

        # Must not raise even though there is no `_sqlite_backend` at all.
        reconcile_stale_quarantine_rows(_NoBackendManager())

    def test_golden_repo_list_failure_is_a_silent_no_op(self, backend):
        class _FailingListManager:
            def __init__(self, sqlite_backend):
                self._sqlite_backend = sqlite_backend

            def list_golden_repos(self):
                raise RuntimeError("simulated registry outage")

        manager = _FailingListManager(backend)
        record_unrecoverable_corruption(manager, "evolution", "corrupt detail")

        # Must not raise; row is left untouched since we cannot safely
        # tell live-vs-orphaned without the golden repo list.
        reconcile_stale_quarantine_rows(manager)

        assert get_failure_state(manager, "evolution") is not None
