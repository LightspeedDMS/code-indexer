"""Bug #1761: FTS search_code returns duplicated result rows for a file
reprocessed across two separate indexing passes that reuse the same
on-disk Tantivy FTS index (e.g. golden-repo registration followed by an
activation branch-delta reindex, or any repeated `cidx index` run with
create_new_fts=False).

Root cause: FileChunkingManager's per-file FTS block
(file_chunking_manager.py, `if self.fts_manager: ... self.fts_manager.add_document(fts_doc)`)
unconditionally ADDS new FTS documents for every chunk of a processed
file, with no delete-by-path supersession step first. Unlike the vector
store -- where re-indexing identical content upserts the SAME
deterministic point_id, naturally deduplicating -- Tantivy's
add_document() has no document-id concept: every call appends a brand
new document. Reprocessing an already-FTS-indexed, unchanged file
(opening the SAME on-disk Tantivy index a second time with
create_new=False) therefore leaves the OLD chunk documents in place
alongside the NEW ones, producing byte-identical duplicate search
results that differ only by BM25 score (matches the reported symptom:
score 1.0 vs 0.99).

This test proves the defect end-to-end through the REAL indexing
components (FileChunkingManager + TantivyIndexManager), not just the
FTS layer in isolation, and must FAIL on the pre-fix code (2 results
for a token with exactly 1 real occurrence) and PASS after the fix (1
result).
"""

# mypy: ignore-errors
# Duck-typed fakes passed to FileChunkingManager's strictly-typed
# constructor params (vector_manager: VectorCalculationManager, etc.)
# work correctly at runtime but trip mypy's structural check -- matches
# the established convention in the sibling test_file_chunking_manager_1502.py.

import hashlib
import threading
from concurrent.futures import Future
from pathlib import Path
from typing import Any, Dict, List
from unittest.mock import Mock

from code_indexer.services.clean_slot_tracker import CleanSlotTracker
from code_indexer.services.file_chunking_manager import FileChunkingManager
from code_indexer.services.tantivy_index_manager import TantivyIndexManager


class _FakeVectorManager:
    """Minimal VectorCalculationManager stand-in returning a deterministic,
    always-truthy embedding per chunk (mirrors the established pattern in
    test_file_chunking_manager_1502.py)."""

    def __init__(self) -> None:
        self.cancellation_event = threading.Event()
        self.embedding_provider = Mock()
        self.embedding_provider.get_current_model.return_value = "voyage-large-2"
        self.embedding_provider._get_model_token_limit.return_value = 120000
        self.batches: List[List[str]] = []

    def submit_batch_task(
        self, chunk_texts: List[str], metadata: Dict[str, Any]
    ) -> "Future[Any]":
        from code_indexer.services.vector_calculation_manager import VectorResult

        self.batches.append(list(chunk_texts))
        future: "Future[Any]" = Future()
        embeddings = tuple(
            tuple(float(b) / 255.0 for b in hashlib.sha256(text.encode()).digest()[:8])
            for text in chunk_texts
        )
        result = VectorResult(
            task_id=f"batch_{len(self.batches)}",
            embeddings=embeddings,
            metadata=metadata.copy(),
            processing_time=0.0,
            error=None,
        )
        future.set_result(result)
        return future


class _FakeVectorStoreClient:
    """Minimal vector_store_client stand-in: accepts every upsert, reports
    no pre-existing content hashes (always a fresh/full embed)."""

    def __init__(self) -> None:
        self.upserted_points: List[Dict[str, Any]] = []

    def upsert_points(self, points: List[Dict[str, Any]], collection_name=None) -> bool:
        self.upserted_points.extend(points)
        return True

    def collection_exists(self, collection_name: str) -> bool:
        return True

    def create_collection(self, collection_name: str, vector_size: int) -> bool:
        return True

    def get_existing_content_hashes(
        self, file_path: str, collection_name: str
    ) -> Dict[int, Dict[str, Any]]:
        return {}


def _make_single_chunk_chunker(unique_token: str) -> Mock:
    """Deterministic chunker returning exactly ONE chunk containing
    `unique_token`, with a fixed line range -- mirrors the real
    FixedSizeChunker's per-chunk dict shape."""
    chunker = Mock()
    chunker.chunk_file.return_value = [
        {
            "text": f"{unique_token} occurs exactly once in this file",
            "chunk_index": 0,
            "total_chunks": 1,
            "size": 40,
            "file_path": None,
            "file_extension": "py",
            "line_start": 2,
            "line_end": 2,
        }
    ]
    return chunker


def _process_file_once(
    codebase_dir: Path,
    test_file: Path,
    fts_manager: TantivyIndexManager,
    unique_token: str,
) -> None:
    """Runs exactly ONE real FileChunkingManager processing pass for
    `test_file`, wired to a REAL TantivyIndexManager (no mocking of the
    FTS layer under test)."""
    with FileChunkingManager(
        vector_manager=_FakeVectorManager(),
        chunker=_make_single_chunk_chunker(unique_token),
        vector_store_client=_FakeVectorStoreClient(),
        thread_count=2,
        slot_tracker=CleanSlotTracker(max_slots=4),
        codebase_dir=codebase_dir,
        fts_manager=fts_manager,
    ) as manager:
        metadata = {
            "project_id": "proj",
            "file_hash": "sha256:abc123",
            "collection_name": "col",
            "git_available": False,
        }
        future = manager.submit_file_for_processing(test_file, metadata, None)
        result = future.result(timeout=10.0)
        assert result.success, f"File processing failed: {result.error}"


class TestBug1761FTSDuplicateOnReindex:
    """Reproduces MCP search_code (FTS mode) returning duplicate rows for
    a unique token, per issue #1761."""

    def test_reprocessing_same_file_across_two_indexing_passes_yields_one_fts_result(
        self, tmp_path: Path
    ) -> None:
        """Simulates two separate `cidx index` passes over the SAME
        unchanged file, both writing into the SAME on-disk Tantivy FTS
        index (pass 2 opens with create_new=False, exactly as
        SmartIndexer does for any run that finds an existing FTS index)
        -- the real-world trigger for golden-repo registration followed
        by an activation branch-delta reindex, or any repeated indexing
        run.

        Ground truth: the file has exactly ONE occurrence of the unique
        token. search_code (Tantivy FTS) must return exactly ONE result,
        never two byte-identical rows differing only by score.
        """
        unique_token = "ZORPTASTIC"
        codebase_dir = tmp_path
        test_file = codebase_dir / "e2e2_file.py"
        test_file.write_text(f"# line 1\n# {unique_token} occurs exactly once\n")

        fts_index_dir = codebase_dir / ".code-indexer" / "tantivy_index"

        # --- Indexing pass 1: fresh FTS index (golden-repo registration) ---
        fts_manager_pass1 = TantivyIndexManager(fts_index_dir)
        fts_manager_pass1.initialize_index(create_new=True)
        _process_file_once(codebase_dir, test_file, fts_manager_pass1, unique_token)
        fts_manager_pass1.commit()
        fts_manager_pass1.close()

        # --- Indexing pass 2: REUSES the same on-disk index
        # (create_new=False), exactly as SmartIndexer does whenever
        # fts_index_exists is True and force_full is not requested --
        # e.g. an activation branch-delta reindex reprocessing the same
        # already-indexed, unchanged file. ---
        fts_manager_pass2 = TantivyIndexManager(fts_index_dir)
        fts_manager_pass2.initialize_index(create_new=False)
        _process_file_once(codebase_dir, test_file, fts_manager_pass2, unique_token)
        fts_manager_pass2.commit()

        # --- Ground truth: exactly ONE real occurrence of the unique token ---
        results = fts_manager_pass2.search(unique_token, limit=10)

        assert len(results) == 1, (
            f"Expected exactly 1 FTS result for unique token '{unique_token}' "
            f"(file has exactly 1 real occurrence), got {len(results)}: {results}"
        )

        # Discriminating check: no two results may share identical
        # file_path + line_number (the exact duplication shape reported
        # in issue #1761 -- same path, same line, differing only by score).
        seen = set()
        for r in results:
            key = (r["path"], r["line"])
            assert key not in seen, f"Duplicate file_path+line_number result: {r}"
            seen.add(key)


class _FailingDeleteFtsManager:
    """Fake FTS manager whose delete_document_deferred() always raises a
    given exception, used to exercise file_chunking_manager.py's
    code-review CRITICAL 3 fix (silent-failure remediation) without
    needing a real Tantivy index in a failure state. add_document() calls
    are recorded so a test can assert the add loop was (or wasn't)
    reached."""

    def __init__(self, delete_exception: Exception) -> None:
        self._delete_exception = delete_exception
        self.add_document_calls: List[Dict[str, Any]] = []

    def delete_document_deferred(self, file_path: str) -> None:
        raise self._delete_exception

    def add_document(self, doc: Dict[str, Any]) -> None:
        self.add_document_calls.append(doc)


class TestBug1761Critical3SilentFailureRemediation:
    """Code-review CRITICAL 3 regression guard: a failed FTS pre-delete
    must never fall through into the add loop -- doing so silently
    reproduces Bug #1761's exact duplicate-row defect. A transient
    failure must skip the add loop for that file; a genuine wiring error
    (RuntimeError -- writer not initialized) must propagate rather than
    be silently swallowed.
    """

    def test_transient_pre_delete_failure_skips_add_loop_instead_of_duplicating(
        self, tmp_path: Path
    ) -> None:
        codebase_dir = tmp_path
        test_file = codebase_dir / "flaky_file.py"
        test_file.write_text("# marker line\nBOOMTOKEN occurs once\n")

        failing_fts_manager = _FailingDeleteFtsManager(
            delete_exception=ValueError("simulated transient Tantivy failure")
        )

        _process_file_once(codebase_dir, test_file, failing_fts_manager, "BOOMTOKEN")

        assert failing_fts_manager.add_document_calls == [], (
            "A transient FTS pre-delete failure must skip the add loop for "
            "this file entirely -- adding fresh chunks on top of an "
            "undeleted stale document would reproduce Bug #1761's exact "
            "duplicate-row defect"
        )

    def test_wiring_error_from_pre_delete_propagates_rather_than_being_swallowed(
        self, tmp_path: Path
    ) -> None:
        codebase_dir = tmp_path
        test_file = codebase_dir / "wiring_bug_file.py"
        test_file.write_text("# marker line\nZAPTOKEN occurs once\n")

        failing_fts_manager = _FailingDeleteFtsManager(
            delete_exception=RuntimeError(
                "Index writer not initialized. Call initialize_index() first."
            )
        )

        with FileChunkingManager(
            vector_manager=_FakeVectorManager(),
            chunker=_make_single_chunk_chunker("ZAPTOKEN"),
            vector_store_client=_FakeVectorStoreClient(),
            thread_count=2,
            slot_tracker=CleanSlotTracker(max_slots=4),
            codebase_dir=codebase_dir,
            fts_manager=failing_fts_manager,
        ) as manager:
            metadata = {
                "project_id": "proj",
                "file_hash": "sha256:abc123",
                "collection_name": "col",
                "git_available": False,
            }
            future = manager.submit_file_for_processing(test_file, metadata, None)
            result = future.result(timeout=10.0)

        assert not result.success, (
            "A wiring/lifecycle RuntimeError from the FTS pre-delete must "
            "propagate as a processing failure, not be silently swallowed"
        )
        assert "Index writer not initialized" in (result.error or "")
        assert failing_fts_manager.add_document_calls == []
