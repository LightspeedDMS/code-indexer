"""
Unit tests for GoldenRepoMetadataSqliteBackend's registry-reconcile
auto-heal event persistence (GitHub Issue #1383).

Issue #1383 (follow-up to Bug #1382): the circuit-breaker's persisted
confirmation counter is cleared the instant a confirmed auto-removal
completes, so the "3 restarts confirmed, N repos auto-removed" event left
NO persistent trace beyond a log line. These tests cover the new singleton-
row table (`golden_repo_reconcile_auto_heal_event`) that records the most
recent confirmed auto-removal event (removed aliases + timestamp) so it
remains independently queryable/discoverable even after the breaker-state
counter is reset -- mirrors the golden_repo_reconcile_breaker_state
convention (Bug #1382) for consistency.
"""

import os
import tempfile

import pytest

from code_indexer.server.storage.sqlite_backends import GoldenRepoMetadataSqliteBackend


@pytest.fixture
def backend():
    with tempfile.TemporaryDirectory() as temp_dir:
        db_path = os.path.join(temp_dir, "test.db")
        be = GoldenRepoMetadataSqliteBackend(db_path)
        be.ensure_table_exists()
        yield be


class TestReconcileAutoHealEventNeverRecorded:
    def test_get_returns_none_when_never_recorded(self, backend):
        """No auto-heal event has ever fired -- must return None, not an
        empty/zeroed dict, so callers can distinguish 'never happened' from
        'happened with zero aliases'."""
        assert backend.get_reconcile_auto_heal_event() is None


class TestReconcileAutoHealEventRecordAndRetrieve:
    def test_record_and_get_roundtrip(self, backend):
        backend.record_reconcile_auto_heal_event(["alias-a", "alias-b"])

        event = backend.get_reconcile_auto_heal_event()

        assert event is not None
        assert sorted(event["removed_aliases"]) == ["alias-a", "alias-b"]
        assert event["occurred_at"] is not None

    def test_record_single_alias(self, backend):
        backend.record_reconcile_auto_heal_event(["solo-alias"])

        event = backend.get_reconcile_auto_heal_event()

        assert event["removed_aliases"] == ["solo-alias"]

    def test_record_overwrites_previous_event(self, backend):
        """Only the MOST RECENT auto-heal event needs to be discoverable
        (a singleton row, like golden_repo_reconcile_breaker_state) -- a
        new confirmed removal replaces the prior record."""
        backend.record_reconcile_auto_heal_event(["old-alias"])
        backend.record_reconcile_auto_heal_event(["new-alias-1", "new-alias-2"])

        event = backend.get_reconcile_auto_heal_event()

        assert sorted(event["removed_aliases"]) == ["new-alias-1", "new-alias-2"]

    def test_occurred_at_advances_on_overwrite(self, backend):
        backend.record_reconcile_auto_heal_event(["old-alias"])
        first_event = backend.get_reconcile_auto_heal_event()

        backend.record_reconcile_auto_heal_event(["new-alias"])
        second_event = backend.get_reconcile_auto_heal_event()

        assert second_event["occurred_at"] >= first_event["occurred_at"]


class TestReconcileAutoHealEventTableIdempotent:
    def test_ensure_table_exists_is_idempotent(self, backend):
        """Calling ensure_table_exists() a second time (server restart)
        must not raise or wipe existing data."""
        backend.record_reconcile_auto_heal_event(["persisted-alias"])

        backend.ensure_table_exists()

        event = backend.get_reconcile_auto_heal_event()
        assert event is not None
        assert event["removed_aliases"] == ["persisted-alias"]
