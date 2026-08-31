"""
Unit tests for Story #1589's `clear_all_dedup_states` wrapper function in
dedup_state.py -- the service-layer entry point the Diagnostics tab's
"Clear All Dedup Warnings" REST endpoint calls.

Mirrors test_dedup_state_1560.py's exact fixture/double conventions: a REAL
SQLite backend for happy-path coverage, and a minimal
_AlwaysFailingWriteBackend double for exception-translation coverage (this
wrapper's own job is translating a raw backend exception into
DedupStateUnavailableError, identical regardless of the underlying cause).
"""

import os
import tempfile

import pytest

from code_indexer.server.services.fleet_migration.dedup_state import (
    DedupStateUnavailableError,
    clear_all_dedup_states,
    get_dedup_state,
    record_dedup_outcome,
)
from code_indexer.server.storage.sqlite_backends import GoldenRepoMetadataSqliteBackend

_CLEAR_ALL_REASON = "manually acknowledged via Diagnostics tab"


class _FakeGoldenRepoManagerWithBackend:
    def __init__(self, sqlite_backend):
        self._sqlite_backend = sqlite_backend


class _AlwaysFailingWriteBackend:
    """Mirrors test_dedup_state_1560.py's _AlwaysFailingBackend convention
    -- a minimal double implementing only the method under test, raising
    to simulate a persistent backend write outage."""

    def clear_all_dedup_states(self, reason):
        raise RuntimeError("simulated persistent backend write outage")


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


class TestClearAllDedupStatesRoundTrip:
    def test_clears_active_rows_via_real_backend_and_returns_count(self, backend):
        manager = _FakeGoldenRepoManagerWithBackend(backend)
        record_dedup_outcome(
            manager,
            "click",
            duplicate_groups=1,
            records_before=10,
            records_deleted=1,
            winner_kept_groups=1,
            whole_group_deleted_groups=0,
            collection_total=10,
        )
        record_dedup_outcome(
            manager,
            "evolution",
            duplicate_groups=2,
            records_before=20,
            records_deleted=2,
            winner_kept_groups=2,
            whole_group_deleted_groups=0,
            collection_total=20,
        )

        cleared_count = clear_all_dedup_states(manager, _CLEAR_ALL_REASON)

        assert cleared_count == 2
        # Prove the rows were ACTUALLY cleared through the real backend
        # (not merely that a count was returned).
        for alias in ("click", "evolution"):
            state = get_dedup_state(manager, alias)
            assert state is not None
            assert state["cleared_at"] is not None
            assert state["cleared_reason"] == _CLEAR_ALL_REASON
        # A second call now finds nothing active -- proves the first call
        # genuinely persisted the clear rather than being a no-op count.
        assert clear_all_dedup_states(manager, _CLEAR_ALL_REASON) == 0


class TestClearAllDedupStatesInputValidation:
    def test_rejects_empty_reason(self, backend):
        manager = _FakeGoldenRepoManagerWithBackend(backend)
        with pytest.raises(ValueError):
            clear_all_dedup_states(manager, "")


class TestClearAllDedupStatesBackendFailurePropagation:
    def test_propagates_dedup_state_unavailable_on_write_failure(self):
        manager = _FakeGoldenRepoManagerWithBackend(_AlwaysFailingWriteBackend())
        with pytest.raises(DedupStateUnavailableError):
            clear_all_dedup_states(manager, _CLEAR_ALL_REASON)


class TestClearAllDedupStatesNoBackendConfigured:
    def test_returns_zero_when_no_backend_configured(self):
        class _NoBackendManager:
            pass

        assert clear_all_dedup_states(_NoBackendManager(), _CLEAR_ALL_REASON) == 0
