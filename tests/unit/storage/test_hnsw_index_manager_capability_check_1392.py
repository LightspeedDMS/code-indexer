"""Tests for Bug #1392: hnswlib fork-capability check on the CLI (storage)
side -- historical context + design reversal note.

Original Bug #1392: the CLI's separate system-wide Python environment can
drift to a stock PyPI hnswlib (missing check_integrity()/repair_orphans())
while the server's own pipx venv stays on the custom fork. Every
finalize-time `_detect_and_repair_orphans()` call then failed with a bare
AttributeError deep inside `build_index`/`rebuild_from_vectors`/
`save_incremental_update`, after heavy indexing work had already run.

Bug #1392's original fix added `_ensure_hnswlib_capability()`, raising a new
`HNSWCapabilityError` as the VERY FIRST statement of those three
build/finalize methods -- immediately, before any indexing work, rather than
deep inside `_detect_and_repair_orphans()`.

**SUPERSEDED by Bug #1415** (production incident 2026-07-14): that fail-fast
design still ABORTED the entire indexing operation on a drifted hnswlib
install -- a fleet-wide outage (~12 golden repos, plus an activated-repo
branch-delta reindex blocked by Bug #1203's correctness-first design). It
just replaced a raw, late `AttributeError` with an earlier, clearer
exception; either shape crashes indexing wholesale.

`HNSWCapabilityError` and the raising `_ensure_hnswlib_capability()` no
longer exist on this module. They are replaced by a non-raising
`_hnswlib_has_fork_capability()` predicate consulted ONLY by
`_detect_and_repair_orphans()`, which degrades (WARNING + skip the orphan
pass) rather than aborting the caller's build/save -- see
tests/unit/storage/test_hnsw_index_manager_capability_degrade_1415.py for
the full RED/GREEN coverage of that replacement design (missing-capability
degrade, real-fork regression, genuine-failure-still-raises).

This file keeps the still-valid Bug #1392 assertions that survive the
reversal: the predicate reports True against the real installed hnswlib in
this test environment (the fork, per pyproject.toml's pin), the
expected-fork-commit constant, and the "Query Is Everything" invariant that
query-only paths are never gated by capability (true under both the old and
new designs).
"""

from code_indexer.storage.hnsw_index_manager import (
    EXPECTED_HNSWLIB_FORK_COMMIT,
    HNSWIndexManager,
)

# Arbitrary small dimension -- these tests never build/query real vectors,
# only exercise capability-predicate and query-path-construction behavior,
# so the exact value is immaterial (kept tiny purely for readability).
TEST_VECTOR_DIM = 4


class TestQueryPathUnaffected:
    """Regression guard (Query Is Everything invariant): query-only paths
    must NEVER be blocked by a missing hnswlib capability, whether that
    capability check is fail-fast (pre-#1415) or graceful-degrade
    (post-#1415). __init__, index_exists, and is_stale must construct/run
    without raising or even consulting hnswlib capability."""

    def test_index_exists_and_is_stale_not_gated_by_capability(self, tmp_path):
        manager = HNSWIndexManager(
            vector_dim=TEST_VECTOR_DIM, space="cosine"
        )  # must not raise
        assert manager.index_exists(tmp_path) is False  # must not raise
        assert manager.is_stale(tmp_path) is True  # must not raise (no index yet)


class TestCapabilityPredicateRetargeted:
    """RED 2 (retargeted): the replacement non-raising
    `_hnswlib_has_fork_capability()` predicate reports True in this test
    environment, where pyproject.toml pins the real LightspeedDMS fork.
    Missing-capability behavior (predicate False, degrade+WARNING) is
    covered exhaustively by test_hnsw_index_manager_capability_degrade_1415.py
    -- this test only proves the predicate is wired and truthful for the
    normal (fork-present) case."""

    def test_predicate_reports_true_for_real_fork(self):
        manager = HNSWIndexManager(vector_dim=TEST_VECTOR_DIM, space="cosine")
        assert manager._hnswlib_has_fork_capability() is True


class TestExpectedForkCommitConstant:
    """RED 1 (retained): EXPECTED_HNSWLIB_FORK_COMMIT still names the pinned
    fork commit, still consumed by both the CLI-side WARNING message
    (Bug #1415's _detect_and_repair_orphans) and the server-side startup
    probe (server/services/hnswlib_capability_check.py, unchanged)."""

    def test_expected_hnswlib_fork_commit_constant_defined(self):
        assert (
            EXPECTED_HNSWLIB_FORK_COMMIT == "878cfbe585395a8bdd95f593d071f778d2fac457"
        )
