"""
Unit tests for the golden-repo registry-reconcile circuit-breaker's
persisted confirmation state (Bug #1382).

Bug #1317's circuit-breaker (ORPHAN_FRACTION_ABORT_THRESHOLD) refuses to
delete anything when more than half of registered golden repos resolve
absent, on the theory that a high absent-fraction usually means an
infra/mount problem. Bug #1382 found a real staging incident where this
protection was permanently defeated: 8/14 (57%) repos were GENUINE,
persistent registry-orphans (crash-recovery gap: DB recovered, on-disk
clones were not), and the circuit-breaker aborted on every single restart
for ~2 months with no path to resolution.

These tests cover the new persisted cross-restart state that lets the
reconcile sweep distinguish "the SAME high-ratio orphan set observed on
multiple consecutive sweeps, with a healthy base directory every time" (real
orphans -- eventually auto-heal) from "a one-off blip" (still requires the
existing safety-first abort) or "a DIFFERENT orphan set each time" (no
stable signal, never confirms).

Reuses the EXISTING GoldenRepoMetadataSqliteBackend / db_path -- this is the
SAME shared, cluster-aware backend GoldenRepoManager already injects via
StorageFactory (SQLite in solo mode, PostgreSQL in cluster mode), so no new
storage layer or factory wiring is introduced (Messi Rule #4: anti-
duplication).
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


class TestReconcileBreakerStateFirstObservation:
    def test_first_observation_returns_count_one(self, backend):
        """The very first high-ratio observation for a fingerprint must
        return a consecutive count of 1 -- not yet confirmed."""
        count = backend.record_reconcile_breaker_observation("alias-a,alias-b")
        assert count == 1

    def test_get_state_after_first_observation(self, backend):
        backend.record_reconcile_breaker_observation("alias-a,alias-b")
        state = backend.get_reconcile_breaker_state()
        assert state is not None
        assert state["orphan_fingerprint"] == "alias-a,alias-b"
        assert state["consecutive_count"] == 1
        assert state["first_observed_at"] is not None
        assert state["last_observed_at"] is not None

    def test_get_state_returns_none_when_never_observed(self, backend):
        """No breaker observation has ever been recorded -> None, not an
        empty/zeroed dict -- callers (health check) must be able to
        distinguish 'never tripped' from 'tripped with count 0'."""
        assert backend.get_reconcile_breaker_state() is None


class TestReconcileBreakerStateMatchingFingerprint:
    def test_matching_fingerprint_increments_count(self, backend):
        """The SAME orphan-candidate set observed again increments the
        consecutive count -- this is the corroborating evidence that lets
        the reconciler eventually trust a high ratio as real orphans."""
        assert backend.record_reconcile_breaker_observation("a,b,c") == 1
        assert backend.record_reconcile_breaker_observation("a,b,c") == 2
        assert backend.record_reconcile_breaker_observation("a,b,c") == 3

    def test_matching_fingerprint_updates_last_observed_at(self, backend):
        backend.record_reconcile_breaker_observation("a,b,c")
        state1 = backend.get_reconcile_breaker_state()
        backend.record_reconcile_breaker_observation("a,b,c")
        state2 = backend.get_reconcile_breaker_state()
        assert state2["consecutive_count"] == 2
        # first_observed_at must be preserved across repeated observations.
        assert state2["first_observed_at"] == state1["first_observed_at"]


class TestReconcileBreakerStateDifferentFingerprint:
    def test_different_fingerprint_resets_count_to_one(self, backend):
        """A DIFFERENT orphan-candidate set is not corroborating evidence --
        it means the absent-repo shape is unstable, so the count must reset
        rather than accumulate toward confirmation."""
        assert backend.record_reconcile_breaker_observation("a,b,c") == 1
        assert backend.record_reconcile_breaker_observation("a,b,c") == 2
        # Different alias set observed this sweep.
        assert backend.record_reconcile_breaker_observation("x,y,z") == 1

    def test_different_fingerprint_replaces_stored_fingerprint(self, backend):
        backend.record_reconcile_breaker_observation("a,b,c")
        backend.record_reconcile_breaker_observation("x,y,z")
        state = backend.get_reconcile_breaker_state()
        assert state["orphan_fingerprint"] == "x,y,z"
        assert state["consecutive_count"] == 1


class TestReconcileBreakerStateReset:
    def test_reset_clears_state_completely(self, backend):
        """reset_reconcile_breaker_state() must fully clear the persisted
        state -- a subsequent observation starts fresh at count 1, exactly
        like a never-before-seen fingerprint."""
        backend.record_reconcile_breaker_observation("a,b,c")
        backend.record_reconcile_breaker_observation("a,b,c")
        assert backend.get_reconcile_breaker_state()["consecutive_count"] == 2

        backend.reset_reconcile_breaker_state()

        assert backend.get_reconcile_breaker_state() is None
        assert backend.record_reconcile_breaker_observation("a,b,c") == 1

    def test_reset_is_idempotent_when_no_state_exists(self, backend):
        """Calling reset before any observation has ever been recorded must
        not raise."""
        backend.reset_reconcile_breaker_state()
        assert backend.get_reconcile_breaker_state() is None
