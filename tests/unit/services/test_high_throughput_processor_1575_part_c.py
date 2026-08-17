"""TDD test for Bug #1575 Part C item 5 -- failure propagation from
``set_hnsw_branch_context()`` inside ``hide_files_not_in_branch_thread_safe()``.

Pre-Part-C, this call site was ``rebuild_hnsw_filtered()`` wrapped in a
try/except that logged a warning and swallowed ANY failure -- silently
masking a real failure to update the HNSW index's branch-visibility state.
Part C's exact-file-changes list requires this to propagate to the caller
instead. Uses this codebase's own established MagicMock-vector_store_client
pattern (see test_high_throughput_processor_1575_part_a.py's
``_make_processor``) rather than inventing a new test style.
"""

from pathlib import Path
from unittest.mock import MagicMock, Mock

import pytest

from code_indexer.services.high_throughput_processor import HighThroughputProcessor


def _make_processor(tmp_path: Path) -> HighThroughputProcessor:
    mock_embedding_provider = Mock()
    mock_embedding_provider.get_provider_name = Mock(return_value="test-provider")
    mock_embedding_provider.get_current_model = Mock(return_value="test-model")

    config = MagicMock()
    config.codebase_dir = tmp_path
    config.embedding_provider = mock_embedding_provider

    vector_store_client = MagicMock()

    return HighThroughputProcessor(
        config=config,
        embedding_provider=mock_embedding_provider,
        vector_store_client=vector_store_client,
    )


def test_set_hnsw_branch_context_failure_propagates_not_swallowed(tmp_path):
    processor = _make_processor(tmp_path)
    processor.vector_store_client.distinct_content_paths.return_value = set()
    processor.vector_store_client.fetch_points_for_paths.return_value = []
    processor.vector_store_client.set_hnsw_branch_context.side_effect = RuntimeError(
        "simulated failure registering branch-visibility context"
    )

    with pytest.raises(RuntimeError, match="simulated failure"):
        processor.hide_files_not_in_branch_thread_safe(
            branch="main",
            current_files=["src/a.py"],
            collection_name="test_collection",
        )


def test_defect1_branch_context_registered_after_finalization_orphans_session(
    tmp_path,
):
    """Bug #1575 Part C review fix (Defect 1, dual-review corroborated):
    ``process_files_incrementally``'s real call chain --
    ``process_branch_changes_high_throughput(skip_branch_isolation=True)``
    followed by ``hide_files_not_in_branch_thread_safe()`` -- must not
    finalize (``end_indexing()``) the indexing session BEFORE the
    branch-isolation context is registered on it. Pre-fix,
    ``process_branch_changes_high_throughput``'s own ``finally`` block
    always finalizes immediately, so the branch context
    ``hide_files_not_in_branch_thread_safe`` registers afterward lands on a
    session nothing will ever finalize -- a ghost vector: a ``FilesystemVectorStore``
    point excluded from the NEW branch remains reachable via a REAL HNSW
    search because the HNSW graph was never rebuilt/filtered using that
    context.

    The fix adds ``defer_finalization`` to
    ``process_branch_changes_high_throughput`` (skips its own finalization)
    plus a shared ``_finalize_indexing_session`` helper the caller invokes
    itself AFTER establishing branch-isolation context -- exactly mirroring
    the corrected sequence ``smart_indexer.process_files_incrementally``
    must use.
    """
    from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

    vector_dim = 16
    store = FilesystemVectorStore(base_path=tmp_path / "index")
    collection_name = "test_coll"
    store.create_collection(collection_name, vector_size=vector_dim)

    # Seed a clean, PRE-EXISTING index (as if built while on the OLD branch)
    # containing a point for a file that only exists on that old branch.
    old_only_vector = [0.9] * vector_dim
    store.begin_indexing(collection_name)
    store.upsert_points(
        collection_name,
        [
            {
                "id": "old_only_chunk",
                "vector": old_only_vector,
                "payload": {
                    "path": "old_only.py",
                    "type": "content",
                    "hidden_branches": [],
                },
            }
        ],
    )
    store.end_indexing(collection_name)

    # Sanity: the seed point is really in the HNSW graph before the fixed
    # sequence runs.
    baseline_results = store.search(
        query="",
        embedding_provider=Mock(),
        collection_name=collection_name,
        precomputed_query_vector=old_only_vector,
        limit=5,
    )
    assert any(
        r["id"] == "old_only_chunk" for r in baseline_results
    ), "test setup invalid: seed point not found in baseline HNSW search"

    processor = _make_processor(tmp_path)
    processor.vector_store_client = store

    # Real call chain, mirroring the CORRECTED smart_indexer.py
    # process_files_incrementally() sequence: defer finalization so the
    # SAME finalization pass that closes the session also consumes this
    # refresh's branch-isolation context.
    processor.process_branch_changes_high_throughput(
        old_branch="",
        new_branch="new-branch",
        changed_files=[],
        unchanged_files=[],
        collection_name=collection_name,
        skip_branch_isolation=True,
        defer_finalization=True,
    )
    try:
        processor.hide_files_not_in_branch_thread_safe(
            branch="new-branch",
            current_files=[],
            collection_name=collection_name,
        )
    finally:
        processor._finalize_indexing_session(collection_name)

    # REAL HNSW search: old_only_chunk must no longer be reachable once the
    # branch-isolation cycle has hidden it -- proving the branch-filter
    # context was actually consumed by a rebuild/update for THIS refresh,
    # not orphaned on a session nothing will ever finalize.
    results = store.search(
        query="",
        embedding_provider=Mock(),
        collection_name=collection_name,
        precomputed_query_vector=old_only_vector,
        limit=5,
    )
    result_ids = {r["id"] for r in results}
    assert "old_only_chunk" not in result_ids, (
        "ghost vector: old_only_chunk is still reachable via a real HNSW "
        "search after branch isolation should have hidden it -- the "
        "branch-isolation context was registered on a session orphaned by "
        "an earlier end_indexing() call that already ran before the "
        "context existed."
    )
