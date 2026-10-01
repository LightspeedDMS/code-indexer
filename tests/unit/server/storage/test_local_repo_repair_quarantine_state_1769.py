"""
Unit tests for the local-repo `cidx init` repair per-repo failure
quarantine persisted state (Bug #1769).

Mirrors Bug #1506's `refresh_integrity_quarantine_state` shape exactly
(same backend injection convention -- `GoldenRepoMetadataSqliteBackend` /
`GoldenRepoMetadataPostgresBackend`, SQLite solo / PostgreSQL cluster).
This domain is structurally identical to Bug #1506's: RefreshScheduler
attempts the SAME repair operation (`cidx init --force`) every scheduled
cycle for a given golden_alias and naturally alternates try/reset -- a
bare consecutive counter (increment on failure, reset to zero on any
success) is sufficient, matching Bug #1506's own "deliberately simpler
than fleet_migration_quarantine_state" design note.

Before this fix, `RefreshScheduler._repair_uninitialized_local_repo()`
had NO persisted failure state at all -- a permanently-broken local repo
(e.g. `langfuse_Claude_Code_*-global`) re-ran `cidx init --force` on
every single scheduled refresh cycle forever, logging an ERROR each time
with zero convergence (observed: 1,151 occurrences over 3+ days on
staging). This new table lets RefreshScheduler detect N consecutive
confirmed failures and stop retrying, mirroring Bug #1506's circuit
breaker for the sibling "ordinary refresh keeps failing identically"
class of defect.

New sibling table `local_repo_repair_quarantine_state`, keyed per
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


class TestRecordLocalRepoRepairFailure:
    def test_first_failure_returns_count_one(self, backend):
        count = backend.record_local_repo_repair_failure(
            "langfuse_Claude_Code_seba-global",
            "cidx init exited 1: no configuration found",
        )
        assert count == 1

    def test_get_state_after_first_failure(self, backend):
        backend.record_local_repo_repair_failure(
            "langfuse_Claude_Code_seba-global", "detail-1"
        )
        state = backend.get_local_repo_repair_failure_state(
            "langfuse_Claude_Code_seba-global"
        )
        assert state is not None
        assert state["golden_alias"] == "langfuse_Claude_Code_seba-global"
        assert state["consecutive_failure_count"] == 1
        assert state["last_detail"] == "detail-1"
        assert state["first_failed_at"] is not None
        assert state["last_failed_at"] is not None

    def test_get_state_returns_none_when_never_failed(self, backend):
        assert backend.get_local_repo_repair_failure_state("never-failed") is None

    def test_consecutive_failures_accumulate(self, backend):
        backend.record_local_repo_repair_failure("repo-a-global", "detail-1")
        backend.record_local_repo_repair_failure("repo-a-global", "detail-2")
        count = backend.record_local_repo_repair_failure("repo-a-global", "detail-3")

        assert count == 3
        state = backend.get_local_repo_repair_failure_state("repo-a-global")
        assert state["consecutive_failure_count"] == 3
        # Detail is always overwritten to the MOST RECENT failure.
        assert state["last_detail"] == "detail-3"

    def test_rejects_empty_golden_alias(self, backend):
        with pytest.raises(ValueError):
            backend.record_local_repo_repair_failure("", "some detail")

    def test_rejects_empty_detail(self, backend):
        with pytest.raises(ValueError):
            backend.record_local_repo_repair_failure("repo-a-global", "")

    def test_failures_are_independent_per_alias(self, backend):
        backend.record_local_repo_repair_failure("repo-a-global", "detail-1")
        backend.record_local_repo_repair_failure("repo-b-global", "detail-x")
        backend.record_local_repo_repair_failure("repo-b-global", "detail-y")

        a_state = backend.get_local_repo_repair_failure_state("repo-a-global")
        b_state = backend.get_local_repo_repair_failure_state("repo-b-global")
        assert a_state["consecutive_failure_count"] == 1
        assert b_state["consecutive_failure_count"] == 2


class TestResetLocalRepoRepairFailure:
    def test_reset_clears_state_entirely(self, backend):
        backend.record_local_repo_repair_failure("repo-a-global", "detail-1")
        backend.reset_local_repo_repair_failure("repo-a-global")

        assert backend.get_local_repo_repair_failure_state("repo-a-global") is None

    def test_reset_on_never_failed_alias_is_a_no_op(self, backend):
        # Must not raise.
        backend.reset_local_repo_repair_failure("never-failed")
        assert backend.get_local_repo_repair_failure_state("never-failed") is None

    def test_reset_then_new_failure_starts_at_one_again(self, backend):
        backend.record_local_repo_repair_failure("repo-a-global", "detail-1")
        backend.record_local_repo_repair_failure("repo-a-global", "detail-2")
        backend.reset_local_repo_repair_failure("repo-a-global")

        count = backend.record_local_repo_repair_failure("repo-a-global", "detail-3")
        assert count == 1

    def test_reset_rejects_empty_alias(self, backend):
        with pytest.raises(ValueError):
            backend.reset_local_repo_repair_failure("")


class TestGetLocalRepoRepairFailureStateValidation:
    def test_get_state_rejects_empty_alias(self, backend):
        with pytest.raises(ValueError):
            backend.get_local_repo_repair_failure_state("")
