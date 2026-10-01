"""Bug #1746 Change 3 (extended per code-review finding B2):
process_branch_changes_high_throughput -- and the shared
_finalize_indexing_session() helper it (and process_files_incrementally)
uses -- must call abort_indexing() instead of end_indexing() when a fatal
ChunkStoreUnavailableError propagates from process_files_high_throughput(),
exactly like the three SmartIndexer entry points already fixed
(_do_full_index, _do_incremental_index, _do_resume_interrupted).

Root cause (code review finding, confirmed): process_branch_changes_high_
throughput's own `finally` block called self._finalize_indexing_session()
(-> end_indexing()) UNCONDITIONALLY, even when the exception it just
re-raised was the fatal chunk-store error -- silently advancing the
watermark / persisting "indexing complete" state for a run that never
actually indexed the repository, on the branch-change-detection and
reconcile code paths (both call this method with defer_finalization=False,
the default).

NOTE on process_files_high_throughput being patched here: it is a
collaborator of the method under test (process_branch_changes_high_
throughput), not the SUT itself -- the SUT is this method's own
exception-handling/finalization decision (abort vs. end). This is the
SAME established pattern already used, unchallenged, in this same
session's tests/unit/services/test_smart_indexer_1746_abort_on_fatal_error.py
(patches process_files_high_throughput on SmartIndexer instances for the
identical reason). process_files_high_throughput itself has its own
dedicated real-pipeline test coverage proving it genuinely raises
ChunkStoreUnavailableError (tests/unit/services/
test_file_chunking_manager_1746.py, tests/integration/services/
test_high_throughput_processor_1746.py).
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.services.high_throughput_processor import HighThroughputProcessor
from code_indexer.storage.sqlite_chunk_store import ChunkStoreUnavailableError


def _make_processor(tmp_path: Path, store: MagicMock) -> HighThroughputProcessor:
    config = MagicMock()
    config.codebase_dir = tmp_path
    config.exclude_dirs = []
    config.exclude_patterns = []

    embedding_provider = MagicMock()
    embedding_provider.get_current_model.return_value = "voyage-code-3"
    embedding_provider.get_provider_name.return_value = "voyage-ai"

    return HighThroughputProcessor(
        config=config,
        embedding_provider=embedding_provider,
        vector_store_client=store,
    )


@pytest.fixture
def mock_vector_store() -> MagicMock:
    store = MagicMock()
    store.begin_indexing.return_value = None
    store.end_indexing.return_value = {"vectors_indexed": 0}
    store.collection_exists.return_value = False
    return store


class TestFinalizeIndexingSessionAbortParameter:
    """Direct unit coverage of the shared helper: given
    fatal_chunk_store_error is not None, abort_indexing() is called and
    end_indexing() is not; given it is None, behavior is unchanged."""

    def test_fatal_error_calls_abort_not_end(
        self, tmp_path: Path, mock_vector_store: MagicMock
    ) -> None:
        processor = _make_processor(tmp_path, mock_vector_store)

        processor._finalize_indexing_session(
            "test_collection",
            fatal_chunk_store_error=ChunkStoreUnavailableError("chunks.db unwritable"),
        )

        mock_vector_store.abort_indexing.assert_called_once_with("test_collection")
        mock_vector_store.end_indexing.assert_not_called()

    def test_no_fatal_error_calls_end_not_abort(
        self, tmp_path: Path, mock_vector_store: MagicMock
    ) -> None:
        processor = _make_processor(tmp_path, mock_vector_store)

        processor._finalize_indexing_session("test_collection")

        mock_vector_store.end_indexing.assert_called_once()
        mock_vector_store.abort_indexing.assert_not_called()


class TestProcessBranchChangesAbortsOnFatalError:
    """AC (extended Change 3): process_branch_changes_high_throughput
    (defer_finalization=False, the default -- used by smart_index()'s
    branch-optimization path and _do_reconcile_with_database) must abort,
    not finalize, when the fatal chunk-store error propagates."""

    def test_fatal_chunk_store_error_calls_abort_not_end(
        self, tmp_path: Path, mock_vector_store: MagicMock
    ) -> None:
        processor = _make_processor(tmp_path, mock_vector_store)
        (tmp_path / "a.py").write_text("x = 1\n")

        with patch.object(
            processor,
            "process_files_high_throughput",
            side_effect=ChunkStoreUnavailableError("chunks.db unwritable"),
        ):
            with pytest.raises(ChunkStoreUnavailableError):
                processor.process_branch_changes_high_throughput(
                    old_branch="",
                    new_branch="master",
                    changed_files=["a.py"],
                    unchanged_files=[],
                    collection_name="test_collection",
                )

        mock_vector_store.abort_indexing.assert_called_once_with("test_collection")
        mock_vector_store.end_indexing.assert_not_called()

    def test_non_fatal_failure_still_calls_end_not_abort(
        self, tmp_path: Path, mock_vector_store: MagicMock
    ) -> None:
        """Regression: an ordinary (non-storage) failure must be
        byte-identical to pre-#1746 behavior."""
        processor = _make_processor(tmp_path, mock_vector_store)
        (tmp_path / "a.py").write_text("x = 1\n")

        with patch.object(
            processor,
            "process_files_high_throughput",
            side_effect=RuntimeError("embedding provider exploded"),
        ):
            with pytest.raises(RuntimeError):
                processor.process_branch_changes_high_throughput(
                    old_branch="",
                    new_branch="master",
                    changed_files=["a.py"],
                    unchanged_files=[],
                    collection_name="test_collection",
                )

        mock_vector_store.end_indexing.assert_called_once()
        mock_vector_store.abort_indexing.assert_not_called()
