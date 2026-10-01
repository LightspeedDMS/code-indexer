"""Bug #1871 ALSO IN SCOPE item 5: finalize-vs-abort classification for an
ordinary FileNotFoundError -- the exact exception type the incremental
filesystem walk raises when a concurrent reader deletes a snapshot-reader
lease file between enumeration and stat (Bug #1871's root defect #2: "the
incremental filesystem walk enumerates a lease file that a concurrent
reader deletes before it is stat'd, raising FileNotFoundError, which
hard-aborts the entire golden-repo refresh").

``smart_indexer.py``'s ``_do_incremental_index`` sets
``fatal_chunk_store_error`` ONLY for ``ChunkStoreUnavailableError`` (Bug
#1746 Change 3) -- deliberately, per that bug's own regression test
(``test_non_fatal_failure_still_calls_end_not_abort``,
``tests/unit/services/test_smart_indexer_1746_abort_on_fatal_error.py``).
An ordinary ``FileNotFoundError`` therefore reaches the method's
``finally`` block with ``fatal_chunk_store_error=None`` and calls
``end_indexing()`` (finalize, watermark advances), NOT
``abort_indexing()``.

INVESTIGATIVE ONLY per the mission: ``smart_indexer.py`` is outside this
bug's owned file list. This test proves the CURRENT behavior with an
assertion, not prose, and states a verdict below -- it does not change
``smart_indexer.py``.

Why ``process_files_high_throughput`` is patched (not the actual
FileFinder walk): this mirrors, verbatim, the technique the existing,
already-reviewed Bug #1746 test suite established for testing this exact
class of behavior. That suite's own module docstring states the
rationale directly: "process_files_high_throughput being patched here...
is a collaborator of the methods under test (_do_full_index et al.), not
the SUT itself -- the SUT is these methods' own exception-handling/
finalization decision (abort vs. end)." The same reasoning applies here:
the mission asks only to prove that "an ordinary FileNotFoundError after
begin_indexing()" is classified non-fatal -- not to reproduce the exact
unowned internal call site (a later, separate ``self.file_finder.
find_files()`` walk used for branch-isolation housekeeping) where the
real production race happens. Patching the collaborator lets the
FileNotFoundError arrive at the SAME point in the method's control flow
(inside the `try`, after `begin_indexing()`, before the `finally`) that
the real race would reach it at, without re-testing FileFinder/glob
internals this pair does not own.

VERDICT (full reasoning restated in the pair's final handoff): CORRECT AS
DESIGNED, not a defect requiring escalation. `process_files_high_
throughput(...)` is batched via `begin_indexing()`, and the real
production race (per Bug #1871's own defect description) occurs in the
LATER branch-isolation `find_files()` walk, which runs only AFTER that
processing has already committed its progress. If that second walk is
what a lease-file race interrupts, the already-processed vectors are
genuine and complete; calling `abort_indexing()` there would discard
valid work over an unrelated, transient, retriable housekeeping race.
`end_indexing()` is the correct choice for that failure shape, and the
exception still propagates to the caller regardless (the golden-repo
refresh still reports failure -- this test's assertion on
`exc_info.value.__cause__` confirms visibility is unaffected; only the
finalize/abort CHOICE differs).

Placed under ``tests/unit/global_repos/`` (an owned test directory)
rather than ``tests/unit/services/`` (not owned for this bug; see
negotiation turns 3-5) -- pytest does not require a test file's location
to mirror the module under test.
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.config import Config
from code_indexer.services.smart_indexer import SmartIndexer


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


class TestFileNotFoundErrorDuringIncrementalIndexFinalizesNotAborts:
    """AC (Bug #1871 item 5, investigative): given an ordinary
    FileNotFoundError -- the exact type a snapshot-reader-lease race
    produces -- interrupts incremental indexing after begin_indexing(),
    end_indexing() is called and abort_indexing() is NOT called, and the
    exception still propagates to the caller (the golden-repo refresh
    still reports failure -- only the finalize/abort CHOICE is under
    test, not whether the failure is surfaced at all)."""

    def _seed_for_incremental(self, indexer: SmartIndexer) -> None:
        meta = indexer.progressive_metadata.metadata
        meta["status"] = "completed"
        meta["last_index_timestamp"] = 1.0  # epoch -- far in the past
        meta["embedding_provider"] = "voyage-ai"
        meta["embedding_model"] = "voyage-code-3"
        meta["files_to_index"] = []
        meta["current_file_index"] = 0
        indexer.progressive_metadata._save_metadata()

    def test_ordinary_file_not_found_error_calls_end_not_abort(
        self, tmp_path: Path, git_repo: Path, mock_vector_store: MagicMock
    ) -> None:
        indexer = _make_indexer(git_repo, tmp_path, mock_vector_store)
        self._seed_for_incremental(indexer)

        lease_race_error = FileNotFoundError(
            2,
            "No such file or directory",
            str(
                git_repo
                / ".."
                / "cidx-meta"
                / ".snapshot-reader-leases"
                / "deadbeef-hostname-123.json"
            ),
        )

        with patch.object(
            indexer,
            "process_files_high_throughput",
            side_effect=lease_race_error,
        ):
            with pytest.raises(
                RuntimeError, match="Git-aware incremental indexing failed"
            ) as exc_info:
                indexer._do_incremental_index(
                    batch_size=50,
                    progress_callback=None,
                    git_status=GIT_STATUS,
                    provider_name="voyage-ai",
                    model_name="voyage-code-3",
                    safety_buffer_seconds=0,
                )

        # The failure still propagates to the caller (the golden-repo
        # refresh still reports failure) -- confirming the finalize
        # decision below is orthogonal to whether the error is surfaced.
        assert exc_info.value.__cause__ is lease_race_error

        # THE ACTUAL VERDICT UNDER TEST: end_indexing() (finalize), never
        # abort_indexing(), for this exception type -- matching Bug
        # #1746's deliberate "fatal == ChunkStoreUnavailableError only"
        # scoping. See the module docstring's VERDICT section for why
        # this is correct behavior for this specific failure shape, not
        # an oversight this pair is escalating.
        mock_vector_store.end_indexing.assert_called_once_with("test_collection", None)
        mock_vector_store.abort_indexing.assert_not_called()
