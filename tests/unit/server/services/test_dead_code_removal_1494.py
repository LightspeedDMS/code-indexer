"""Regression guard for Story #1494 AC5 dead-code deletion.

`DiagnosticsService._get_storage_statistics` (full-tree rglob("*") + per-file
stat(), zero call sites per the GIL-blocking analysis report) is deleted.

`GitSyncExecutor._trigger_cidx_index` is INTENTIONALLY NOT deleted -- during
implementation, re-verification (mandatory per AC5's own text before
deleting) found a live call site: `GitSyncExecutor.sync_repository` calls
`self._trigger_cidx_index()` internally (git_sync_executor.py, inside the
same never-externally-instantiated class). Deleting the method while that
internal caller remains would turn a currently-inert class into one with a
guaranteed AttributeError the moment it is ever wired up -- a worse landmine
than the one AC5 sets out to remove. Per AC5's explicit instruction ("If
re-verification finds ANY live call site, STOP and report it instead of
deleting"), this method is retained and the finding is reported instead.
"""

from __future__ import annotations

from code_indexer.server.services.diagnostics_service import DiagnosticsService
from code_indexer.server.git.git_sync_executor import GitSyncExecutor


class TestDeadCodeRemoval1494:
    def test_get_storage_statistics_deleted(self) -> None:
        """DiagnosticsService no longer defines _get_storage_statistics."""
        assert not hasattr(DiagnosticsService, "_get_storage_statistics")

    def test_trigger_cidx_index_retained_due_to_live_internal_call_site(self) -> None:
        """GitSyncExecutor._trigger_cidx_index is retained: re-verification
        found a live internal call site (sync_repository calls it), so per
        AC5's own STOP instruction it must not be deleted."""
        assert hasattr(GitSyncExecutor, "_trigger_cidx_index")
