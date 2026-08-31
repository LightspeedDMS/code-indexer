"""Bug #1746 Change 3: SmartIndexer must call abort_indexing() -- NOT
end_indexing() -- when a fatal ChunkStoreUnavailableError (Change 1/2)
propagates out of process_files_high_throughput(), across every real entry
point that calls it directly (_do_full_index, _do_incremental_index,
_do_resume_interrupted). The fatal error must propagate UNWRAPPED (not
folded into the generic RuntimeError these methods otherwise wrap every
exception in) so a caller can identify it specifically.

Root cause (production incident, GitHub issue #1746): every one of these
methods' `finally` block unconditionally called end_indexing() -- even on
the exception path -- finalizing/committing the session as if the run
completed normally. For a fatal storage failure this silently advances the
commit watermark and persists "indexing complete" state for a run that
never actually indexed the repository.

AC (regression): ordinary (non-fatal) per-file failures -- including under
the existing failed_files mechanism -- must continue calling end_indexing()
exactly as before this change. Proven directly against _do_full_index.

NOTE on process_files_high_throughput being patched here: it is a
collaborator of the methods under test (_do_full_index et al.), not the SUT
itself -- the SUT is these methods' own exception-handling/finalization
decision (abort vs. end). process_files_high_throughput has its own
dedicated real-pipeline test coverage proving it genuinely raises
ChunkStoreUnavailableError against a real unwritable chunks.db (Change 1:
tests/unit/services/test_file_chunking_manager_1746.py; Change 2:
tests/integration/services/test_high_throughput_processor_1746.py).
Duplicating that full real embedding+chunk-store pipeline here would be
redundant re-testing of an already-proven lower layer. This exact
patch.object(indexer, "process_files_high_throughput", ...) technique is
pre-existing, established convention in this same file's sibling
tests/unit/services/test_smart_indexer.py (see
TestResumeReanchorWiring.test_resume_reanchors_stale_stored_paths_under_codebase_dir).
"""

import subprocess
from pathlib import Path
from typing import Any, Dict
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.config import Config
from code_indexer.services.smart_indexer import SmartIndexer
from code_indexer.storage.sqlite_chunk_store import ChunkStoreUnavailableError


def _create_git_repo(path: Path) -> str:
    subprocess.run(["git", "init", str(path)], check=True, capture_output=True)
    subprocess.run(
        ["git", "-C", str(path), "config", "user.email", "test@test.com"],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "-C", str(path), "config", "user.name", "Test"],
        check=True,
        capture_output=True,
    )
    (path / "initial.py").write_text("# initial\n")
    subprocess.run(
        ["git", "-C", str(path), "add", "."], check=True, capture_output=True
    )
    subprocess.run(
        ["git", "-C", str(path), "commit", "-m", "initial"],
        check=True,
        capture_output=True,
    )
    result = subprocess.run(
        ["git", "-C", str(path), "rev-parse", "HEAD"],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _make_indexer(repo: Path, tmp_path: Path, store: MagicMock) -> SmartIndexer:
    config = Config(codebase_dir=repo)
    mock_embedding = MagicMock()
    metadata_path = tmp_path / "metadata.json"
    return SmartIndexer(
        config=config,
        embedding_provider=mock_embedding,
        vector_store_client=store,
        metadata_path=metadata_path,
    )


def _assert_fatal_error_aborts_not_ends(
    indexer: SmartIndexer,
    store: MagicMock,
    method_name: str,
    kwargs: Dict[str, Any],
) -> None:
    """Shared assertion for every real process_files_high_throughput()
    call site: patch it to raise the fatal error, invoke the named
    SmartIndexer method, and assert the SAME typed error propagates
    unwrapped while abort_indexing() (not end_indexing()) was called."""
    fatal_error = ChunkStoreUnavailableError("chunks.db unwritable")

    with patch.object(
        indexer, "process_files_high_throughput", side_effect=fatal_error
    ):
        with pytest.raises(ChunkStoreUnavailableError) as exc_info:
            getattr(indexer, method_name)(**kwargs)

    assert exc_info.value is fatal_error
    store.abort_indexing.assert_called_once_with("test_collection")
    store.end_indexing.assert_not_called()


@pytest.fixture
def mock_vector_store() -> MagicMock:
    store = MagicMock()
    store.resolve_collection_name.return_value = "test_collection"
    store.ensure_provider_aware_collection.return_value = "test_collection"
    store.count_points.return_value = 0
    store.begin_indexing.return_value = None
    store.end_indexing.return_value = {"vectors_indexed": 0}
    store.collection_exists.return_value = False
    store.delete_by_filter.return_value = True
    store.get_collection_info.return_value = {"points_count": 0}
    store.clear_collection.return_value = None
    return store


@pytest.fixture
def git_repo(tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _create_git_repo(repo)
    return repo


GIT_STATUS = {"git_available": True, "current_branch": "master", "current_commit": None}


class TestFullIndexAbortsOnFatalError:
    """AC: Given full indexing raises the fatal chunk-store error during
    processing, when the outer exception handling runs, then
    abort_indexing() is called and end_indexing() is NOT called -- no
    watermark update occurs.

    AC (regression): given indexing completes normally -- including with a
    non-fatal per-file failure -- end_indexing() is still called exactly
    as today.
    """

    def test_fatal_chunk_store_error_calls_abort_not_end(
        self, tmp_path: Path, git_repo: Path, mock_vector_store: MagicMock
    ) -> None:
        indexer = _make_indexer(git_repo, tmp_path, mock_vector_store)

        _assert_fatal_error_aborts_not_ends(
            indexer,
            mock_vector_store,
            "_do_full_index",
            dict(
                batch_size=50,
                progress_callback=None,
                git_status=GIT_STATUS,
                provider_name="voyage-ai",
                model_name="voyage-code-3",
            ),
        )

    def test_non_fatal_failure_still_calls_end_not_abort(
        self, tmp_path: Path, git_repo: Path, mock_vector_store: MagicMock
    ) -> None:
        """Regression: an ordinary (non-storage) failure must be
        byte-identical to pre-#1746 behavior -- end_indexing() called,
        abort_indexing() never called, and the existing RuntimeError-wrap
        convention is unchanged (only the fatal-error path bypasses it)."""
        indexer = _make_indexer(git_repo, tmp_path, mock_vector_store)

        with patch.object(
            indexer,
            "process_files_high_throughput",
            side_effect=RuntimeError("embedding provider exploded"),
        ):
            with pytest.raises(RuntimeError, match="Git-aware indexing failed"):
                indexer._do_full_index(
                    batch_size=50,
                    progress_callback=None,
                    git_status=GIT_STATUS,
                    provider_name="voyage-ai",
                    model_name="voyage-code-3",
                )

        mock_vector_store.end_indexing.assert_called_once_with("test_collection", None)
        mock_vector_store.abort_indexing.assert_not_called()

    def test_successful_run_still_calls_end_not_abort(
        self, tmp_path: Path, git_repo: Path, mock_vector_store: MagicMock
    ) -> None:
        """Regression: a fully successful run (including one carrying
        ordinary failed_files > 0 via the returned stats, not an
        exception) is untouched -- end_indexing() called, abort_indexing()
        never called."""
        indexer = _make_indexer(git_repo, tmp_path, mock_vector_store)

        from code_indexer.indexing.processor import ProcessingStats

        stats = ProcessingStats()
        stats.files_processed = 3
        stats.failed_files = 2  # ordinary per-file failures, NOT fatal
        stats.chunks_created = 9

        with patch.object(indexer, "process_files_high_throughput", return_value=stats):
            result = indexer._do_full_index(
                batch_size=50,
                progress_callback=None,
                git_status=GIT_STATUS,
                provider_name="voyage-ai",
                model_name="voyage-code-3",
            )

        assert result.failed_files == 2
        mock_vector_store.end_indexing.assert_called_once_with("test_collection", None)
        mock_vector_store.abort_indexing.assert_not_called()


class TestIncrementalIndexAbortsOnFatalError:
    """AC: Given incremental indexing raises the fatal chunk-store error
    during processing, abort_indexing() is called and end_indexing() is
    NOT called."""

    def _seed_for_incremental(self, indexer: SmartIndexer) -> None:
        meta = indexer.progressive_metadata.metadata
        meta["status"] = "completed"
        meta["last_index_timestamp"] = 1.0  # epoch -- far in the past
        meta["embedding_provider"] = "voyage-ai"
        meta["embedding_model"] = "voyage-code-3"
        meta["files_to_index"] = []
        meta["current_file_index"] = 0
        indexer.progressive_metadata._save_metadata()

    def test_fatal_chunk_store_error_calls_abort_not_end(
        self, tmp_path: Path, git_repo: Path, mock_vector_store: MagicMock
    ) -> None:
        indexer = _make_indexer(git_repo, tmp_path, mock_vector_store)
        self._seed_for_incremental(indexer)

        _assert_fatal_error_aborts_not_ends(
            indexer,
            mock_vector_store,
            "_do_incremental_index",
            dict(
                batch_size=50,
                progress_callback=None,
                git_status=GIT_STATUS,
                provider_name="voyage-ai",
                model_name="voyage-code-3",
                safety_buffer_seconds=0,
            ),
        )


class TestResumeInterruptedAbortsOnFatalError:
    """AC: Given a resumed indexing run raises the fatal chunk-store error
    during processing, abort_indexing() is called and end_indexing() is
    NOT called."""

    def _seed_for_resume(self, indexer: SmartIndexer, git_repo: Path) -> None:
        meta = indexer.progressive_metadata.metadata
        meta["status"] = "in_progress"
        meta["files_to_index"] = [str(git_repo / "initial.py")]
        meta["current_file_index"] = 0
        indexer.progressive_metadata._save_metadata()

    def test_fatal_chunk_store_error_calls_abort_not_end(
        self, tmp_path: Path, git_repo: Path, mock_vector_store: MagicMock
    ) -> None:
        indexer = _make_indexer(git_repo, tmp_path, mock_vector_store)
        self._seed_for_resume(indexer, git_repo)

        _assert_fatal_error_aborts_not_ends(
            indexer,
            mock_vector_store,
            "_do_resume_interrupted",
            dict(
                batch_size=50,
                progress_callback=None,
                git_status=GIT_STATUS,
                provider_name="voyage-ai",
                model_name="voyage-code-3",
            ),
        )


def _run_reconcile_with_side_effect(
    indexer: SmartIndexer, side_effect: BaseException
) -> None:
    """Shared invocation for TestReconcileAbortsOnFatalError: patch
    process_branch_changes_high_throughput (a collaborator with its own
    dedicated real coverage in
    tests/unit/services/test_high_throughput_processor_1746_finalize_or_abort.py
    -- the SUT here is _do_reconcile_with_database's own finalization
    decision, not that collaborator) to raise side_effect, then invoke
    _do_reconcile_with_database."""
    with patch.object(
        indexer, "process_branch_changes_high_throughput", side_effect=side_effect
    ):
        indexer._do_reconcile_with_database(
            batch_size=50,
            progress_callback=None,
            git_status=GIT_STATUS,
            provider_name="voyage-ai",
            model_name="voyage-code-3",
            files_count_to_process=None,
        )


class TestReconcileAbortsOnFatalError:
    """AC (extended Change 3, code review finding B2): given reconcile
    raises the fatal chunk-store error during processing, abort_indexing()
    is called and end_indexing() is NOT called -- _do_reconcile_with_database
    must not finalize a run that hit a fatal chunk-store failure just
    because it duplicates its own inline finalize logic separately from
    process_branch_changes_high_throughput's own (already-fixed) finally
    block."""

    def test_fatal_chunk_store_error_calls_abort_not_end(
        self, tmp_path: Path, git_repo: Path, mock_vector_store: MagicMock
    ) -> None:
        indexer = _make_indexer(git_repo, tmp_path, mock_vector_store)
        # Zero indexed files in the DB -> every on-disk file (initial.py)
        # is treated as "missing from DB" -> files_to_index is non-empty
        # -> the reconcile flow reaches process_branch_changes_high_throughput.
        mock_vector_store.scroll_points.return_value = ([], None)
        fatal_error = ChunkStoreUnavailableError("chunks.db unwritable")

        with pytest.raises(ChunkStoreUnavailableError) as exc_info:
            _run_reconcile_with_side_effect(indexer, fatal_error)

        assert exc_info.value is fatal_error
        mock_vector_store.abort_indexing.assert_called_once_with("test_collection")
        mock_vector_store.end_indexing.assert_not_called()

    def test_non_fatal_failure_still_calls_end_not_abort(
        self, tmp_path: Path, git_repo: Path, mock_vector_store: MagicMock
    ) -> None:
        """Regression: an ordinary (non-storage) failure must be
        byte-identical to pre-#1746 behavior."""
        indexer = _make_indexer(git_repo, tmp_path, mock_vector_store)
        mock_vector_store.scroll_points.return_value = ([], None)

        with pytest.raises(RuntimeError):
            _run_reconcile_with_side_effect(
                indexer, RuntimeError("embedding provider exploded")
            )

        mock_vector_store.end_indexing.assert_called_once()
        mock_vector_store.abort_indexing.assert_not_called()


class TestProcessFilesIncrementallyDoesNotSwallowFatalError:
    """AC (extended Change 3, code review finding B2 -- the most serious
    one): process_files_incrementally (the cidx watch/incremental entry
    point) must NOT swallow a fatal ChunkStoreUnavailableError into
    stats.failed_files and return normally -- that reproduces the EXACT
    silent-failure shape #1746 exists to kill, just on a different door.
    The fatal error must propagate to the caller, and abort_indexing()
    (not end_indexing()) must have been called."""

    def test_fatal_chunk_store_error_propagates_not_swallowed(
        self, tmp_path: Path, git_repo: Path, mock_vector_store: MagicMock
    ) -> None:
        indexer = _make_indexer(git_repo, tmp_path, mock_vector_store)
        fatal_error = ChunkStoreUnavailableError("chunks.db unwritable")

        with patch.object(
            indexer,
            "process_branch_changes_high_throughput",
            side_effect=fatal_error,
        ):
            with pytest.raises(ChunkStoreUnavailableError) as exc_info:
                indexer.process_files_incrementally(file_paths=["initial.py"])

        assert exc_info.value is fatal_error
        mock_vector_store.abort_indexing.assert_called_once_with("test_collection")
        mock_vector_store.end_indexing.assert_not_called()

    def test_non_fatal_failure_still_swallowed_into_failed_files(
        self, tmp_path: Path, git_repo: Path, mock_vector_store: MagicMock
    ) -> None:
        """Regression: an ordinary (non-storage) failure must be
        byte-identical to pre-#1746 behavior -- swallowed into
        stats.failed_files, returned normally, no exception raised."""
        indexer = _make_indexer(git_repo, tmp_path, mock_vector_store)

        with patch.object(
            indexer,
            "process_branch_changes_high_throughput",
            side_effect=RuntimeError("embedding provider exploded"),
        ):
            stats = indexer.process_files_incrementally(file_paths=["initial.py"])

        assert stats.failed_files == 1
        mock_vector_store.end_indexing.assert_called_once()
        mock_vector_store.abort_indexing.assert_not_called()
