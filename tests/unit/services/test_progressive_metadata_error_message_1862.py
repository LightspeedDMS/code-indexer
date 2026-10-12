"""Bug #1862: indexing metadata must not retain a stale `error_message`.

INVARIANT (see the reasoning comment on `start_indexing()` in
`progressive_metadata.py` for the canonical statement): `error_message`
describes only the run whose metadata it currently sits in. Any transition
that starts or completes a run must therefore drop the previous run's error
before that transition's own work begins. Two call sites enforce this:
`start_indexing()` and `complete_indexing()`.

Story #2121 retired resume-from-list (`set_files_to_index()` and
`resume_indexing()` are gone): an interrupted run (status in_progress or
failed, including a legacy file holding a leftover `error_message`) is
recovered by a reconcile that starts a fresh run whenever it has work, or
completes at once when it has none -- either transition drops the error.
That route is proved end to end by
`TestLegacyInProgressMetadataErrorClearedByReconcile` in
`tests/unit/infrastructure/test_smart_indexer.py` (it needs a real
`SmartIndexer`, not just `ProgressiveMetadata` in isolation).

These tests use a real temp directory and real JSON persistence (no
filesystem mocking), and each assertion reloads a FRESH `ProgressiveMetadata`
instance from disk rather than inspecting the original in-memory object --
this is what actually proves the persisted JSON changed, matching how the
production bug was discovered (read out of a file, not out of memory).
"""

import time

from code_indexer.services.progressive_metadata import ProgressiveMetadata


def _create_metadata(tmp_path):
    return ProgressiveMetadata(tmp_path / "metadata.json")


class TestCompleteIndexingClearsErrorMessage:
    """Bug #1862: a successful run must not leave a prior failure message behind."""

    def test_complete_indexing_clears_error_message_on_disk(self, tmp_path):
        metadata = _create_metadata(tmp_path)
        metadata.fail_indexing(
            "'hnswlib.Index' object has no attribute 'check_integrity'"
        )
        assert metadata.metadata["error_message"] == (
            "'hnswlib.Index' object has no attribute 'check_integrity'"
        )

        metadata.complete_indexing(time.time())

        # Reload from disk with a fresh instance -- proves persisted JSON,
        # not just the in-memory dict, reflects the removal.
        reloaded = _create_metadata(tmp_path)
        assert "error_message" not in reloaded.metadata
        assert reloaded.metadata["status"] == "completed"


class TestStartIndexingClearsErrorMessage:
    """Bug #1862: an in-progress run must not display the PREVIOUS run's error.

    This covers the window complete_indexing() alone cannot: a crash between
    start_indexing() and complete_indexing() on a run that follows a failed
    one.
    """

    def test_start_indexing_clears_error_message_on_disk(self, tmp_path):
        metadata = _create_metadata(tmp_path)
        metadata.fail_indexing("Process killed")
        assert metadata.metadata["error_message"] == "Process killed"

        metadata.start_indexing("voyage-ai", "voyage-code-3", {"git_available": True})

        reloaded = _create_metadata(tmp_path)
        assert "error_message" not in reloaded.metadata
        assert reloaded.metadata["status"] == "in_progress"
