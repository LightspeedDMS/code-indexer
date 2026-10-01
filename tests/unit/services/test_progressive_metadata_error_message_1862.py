"""Bug #1862: indexing metadata must not retain a stale `error_message`.

INVARIANT (see the reasoning comment on `start_indexing()` in
`progressive_metadata.py` for the canonical statement): `error_message`
describes only the run whose metadata it currently sits in. Any transition
that starts, resumes, records new work for, or completes a run must
therefore drop the previous run's error before that transition's own work
begins. Four call sites currently enforce this: `start_indexing()`,
`set_files_to_index()`, `resume_indexing()`, and `complete_indexing()`.

A production incident was manufactured because `fail_indexing()`'s
`error_message` was never cleared by a later run. The original fix covered
only `start_indexing()`/`complete_indexing()`, closing the crash window
between the start and completion of a fresh run. Two follow-up reviews then
each found one more missed exit:

1. `SmartIndexer._do_resume_interrupted`, entered via
   `can_resume_interrupted_operation()` which explicitly accepts
   `status == "failed"` (Bug #467). Its terminal paths (cancelled-resume,
   re-interrupted resume, or simply reading the file mid-resume) call
   neither `start_indexing()` nor `complete_indexing()`, so the PRIOR run's
   error would otherwise sit next to fresh progress counters indefinitely.
   Closed by the new `resume_indexing()` method; proved RED-then-green by
   `TestResumeIndexingClearsErrorMessage` below.
2. A legacy on-disk `metadata.json` written by PRE-FIX code can hold
   `status == "in_progress"` plus a leftover `error_message` -- a
   combination post-fix code can no longer CREATE (status="in_progress" is
   now written only by `start_indexing()`, which pops the error) but can
   still READ, since an upgrade does not rewrite old files.
   `_do_incremental_index()`/`_do_reconcile_with_database()` only call
   `start_indexing()` when `status != "in_progress"`, so a legacy file
   already at "in_progress" skips that call and its pop. Closed by moving
   the pop into `set_files_to_index()` (called unconditionally by all three
   fresh-work producers: full index, incremental, reconcile); proved by
   `TestLegacyInProgressMetadataErrorClearedBySetFilesToIndex` in
   `tests/unit/infrastructure/test_smart_indexer.py` -- it lives there
   rather than in this file because it needs a real `SmartIndexer` driving
   the real incremental path, not just `ProgressiveMetadata` in isolation.

These tests use a real temp directory and real JSON persistence (no
filesystem mocking), and each assertion reloads a FRESH `ProgressiveMetadata`
instance from disk rather than inspecting the original in-memory object --
this is what actually proves the persisted JSON changed, matching how the
production bug was discovered (read out of a file, not out of memory).
"""

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

        metadata.complete_indexing()

        # Reload from disk with a fresh instance -- proves persisted JSON,
        # not just the in-memory dict, reflects the removal.
        reloaded = _create_metadata(tmp_path)
        assert "error_message" not in reloaded.metadata
        assert reloaded.metadata["status"] == "completed"


class TestStartIndexingClearsErrorMessage:
    """Bug #1862: an in-progress run must not display the PREVIOUS run's error.

    This covers the window complete_indexing() alone cannot: a crash between
    start_indexing() and complete_indexing() on a FRESH run that follows a
    failed one. It does not cover a RESUMED run continuing that same failed
    attempt -- see TestResumeIndexingClearsErrorMessage below for that
    window (a further window, legacy in-progress metadata on the
    incremental/reconcile paths, is covered outside this file -- see the
    module docstring above).
    """

    def test_start_indexing_clears_error_message_on_disk(self, tmp_path):
        metadata = _create_metadata(tmp_path)
        metadata.fail_indexing("Process killed")
        assert metadata.metadata["error_message"] == "Process killed"

        metadata.start_indexing("voyage-ai", "voyage-code-3", {"git_available": True})

        reloaded = _create_metadata(tmp_path)
        assert "error_message" not in reloaded.metadata
        assert reloaded.metadata["status"] == "in_progress"


class TestResumeIndexingClearsErrorMessage:
    """Bug #1862 follow-up: resuming a failed run must not display its own
    prior error indefinitely.

    `_do_resume_interrupted` neither calls `start_indexing()` (which would
    wrongly reset `files_processed`/`chunks_indexed` to 0, corrupting resume
    accounting) nor, on its cancelled-resume or re-interrupted paths, calls
    `complete_indexing()`. `resume_indexing()` is the narrow, resume-specific
    call site that drops the stale error while leaving `status == "failed"`
    intact, matching Bug #467's requirement that
    `can_resume_interrupted_operation()` keep accepting a "failed" status.
    """

    def test_resume_indexing_clears_error_message_on_disk(self, tmp_path):
        # Faithful production order: start -> set files -> process -> fail.
        # (NOT fail -> set_files_to_index -- that never happens within a
        # single run, and set_files_to_index() itself now also clears a
        # PRIOR run's error, per the set_files_to_index follow-up fix.)
        metadata = _create_metadata(tmp_path)
        metadata.start_indexing("voyage-ai", "voyage-code-3", {"git_available": True})
        metadata.set_files_to_index(["a.py", "b.py", "c.py"])
        metadata.mark_file_completed("a.py", chunks_count=5)
        metadata.fail_indexing(
            "'hnswlib.Index' object has no attribute 'check_integrity'"
        )
        assert metadata.metadata["error_message"] == (
            "'hnswlib.Index' object has no attribute 'check_integrity'"
        )

        metadata.resume_indexing()

        # Reload from disk with a fresh instance -- proves persisted JSON,
        # not just the in-memory dict, reflects the removal.
        reloaded = _create_metadata(tmp_path)
        assert "error_message" not in reloaded.metadata
        # Bug #467: status must remain "failed" through the resume so
        # can_resume_interrupted_operation() keeps accepting it.
        assert reloaded.metadata["status"] == "failed"
