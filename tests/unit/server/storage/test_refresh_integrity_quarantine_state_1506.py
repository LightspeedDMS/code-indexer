"""
Unit tests for the ordinary-refresh integrity-gate per-repo failure
quarantine persisted state (Bug #1506).

Mirrors Issue #1477's `fleet_migration_quarantine_state` shape (same
backend injection convention -- `GoldenRepoMetadataSqliteBackend` /
`GoldenRepoMetadataPostgresBackend`, SQLite solo / PostgreSQL cluster) but
deliberately simpler: unlike fleet migration (which retries the SAME repo
every scheduler tick regardless of outcome, needing a content-signature to
distinguish "genuine on-disk change" from "bare retry"), ordinary refresh
naturally alternates try/reset each scheduled cycle -- a bare consecutive
counter (increment on failure, reset to zero on any success) is sufficient
and not an under-engineered shortcut.

New sibling table `refresh_integrity_quarantine_state`, keyed per
golden_alias (many repos tracked independently, not a singleton row).
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


class TestRecordRefreshIntegrityFailure:
    def test_first_failure_returns_count_one(self, backend):
        count = backend.record_refresh_integrity_failure(
            "click-global", "integrity_check failed: page 3 corrupt"
        )
        assert count == 1

    def test_get_state_after_first_failure(self, backend):
        backend.record_refresh_integrity_failure("click-global", "detail-1")
        state = backend.get_refresh_integrity_failure_state("click-global")
        assert state is not None
        assert state["golden_alias"] == "click-global"
        assert state["consecutive_failure_count"] == 1
        assert state["last_detail"] == "detail-1"
        assert state["first_failed_at"] is not None
        assert state["last_failed_at"] is not None

    def test_get_state_returns_none_when_never_failed(self, backend):
        assert backend.get_refresh_integrity_failure_state("never-failed") is None

    def test_consecutive_failures_accumulate(self, backend):
        backend.record_refresh_integrity_failure("click-global", "detail-1")
        backend.record_refresh_integrity_failure("click-global", "detail-2")
        count = backend.record_refresh_integrity_failure("click-global", "detail-3")

        assert count == 3
        state = backend.get_refresh_integrity_failure_state("click-global")
        assert state["consecutive_failure_count"] == 3
        # Detail is always overwritten to the MOST RECENT failure.
        assert state["last_detail"] == "detail-3"

    def test_rejects_empty_golden_alias(self, backend):
        """Codex review Finding 4: SQLite must reject an empty golden_alias
        identically to the PostgreSQL backend (which already raises
        ValueError) -- an empty alias is a caller bug, not data to store."""
        with pytest.raises(ValueError):
            backend.record_refresh_integrity_failure("", "some detail")

    def test_rejects_empty_detail(self, backend):
        """Codex review Finding 4: SQLite must reject an empty detail
        identically to the PostgreSQL backend."""
        with pytest.raises(ValueError):
            backend.record_refresh_integrity_failure("click-global", "")

    def test_failures_are_independent_per_alias(self, backend):
        backend.record_refresh_integrity_failure("click-global", "detail-1")
        backend.record_refresh_integrity_failure("other-global", "detail-x")
        backend.record_refresh_integrity_failure("other-global", "detail-y")

        click_state = backend.get_refresh_integrity_failure_state("click-global")
        other_state = backend.get_refresh_integrity_failure_state("other-global")
        assert click_state["consecutive_failure_count"] == 1
        assert other_state["consecutive_failure_count"] == 2


class TestResetRefreshIntegrityFailure:
    def test_reset_clears_state_entirely(self, backend):
        backend.record_refresh_integrity_failure("click-global", "detail-1")
        backend.reset_refresh_integrity_failure("click-global")

        assert backend.get_refresh_integrity_failure_state("click-global") is None

    def test_reset_on_never_failed_alias_is_a_no_op(self, backend):
        # Must not raise.
        backend.reset_refresh_integrity_failure("never-failed")
        assert backend.get_refresh_integrity_failure_state("never-failed") is None

    def test_reset_then_new_failure_starts_at_one_again(self, backend):
        backend.record_refresh_integrity_failure("click-global", "detail-1")
        backend.record_refresh_integrity_failure("click-global", "detail-2")
        backend.reset_refresh_integrity_failure("click-global")

        count = backend.record_refresh_integrity_failure("click-global", "detail-3")
        assert count == 1

    def test_reset_rejects_empty_alias(self, backend):
        """Codex review Finding 4: matches PostgreSQL's ValueError on an
        empty alias."""
        with pytest.raises(ValueError):
            backend.reset_refresh_integrity_failure("")


class TestGetRefreshIntegrityFailureStateValidation:
    def test_get_state_rejects_empty_alias(self, backend):
        """Codex review Finding 4: matches PostgreSQL's ValueError on an
        empty alias."""
        with pytest.raises(ValueError):
            backend.get_refresh_integrity_failure_state("")
