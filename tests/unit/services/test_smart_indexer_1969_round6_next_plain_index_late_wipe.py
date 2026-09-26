"""A wipe during finalization must be replayed by the next plain index run.

The triggering run below has a delete-only git delta. Its duplicate and corrupt
ID index appear at the final ID-index load in ``end_indexing()``, after the
run's pending-path checks have finished. The victim file has no git or mtime
change on the next run, so only the durable replay marker can select it.
"""

import inspect
import json
from pathlib import Path

from tests.unit.services.test_smart_indexer_1969_round4_incremental_reprocess import (
    SEARCH_RESULT_LIMIT,
    _corrupt_id_index_bin,
    _deterministic_embedding,
    _duplicate_indexed_record,
    _init_repo,
    _make_smart_indexer,
    _run_git,
)


def _vector_files_for_path(collection_path: Path, rel_path: str) -> list[Path]:
    return [
        vector_file
        for vector_file in collection_path.rglob("vector_*.json")
        if json.loads(vector_file.read_text()).get("payload", {}).get("path")
        == rel_path
    ]


def test_next_plain_incremental_run_restores_end_indexing_finally_wipe(
    tmp_path: Path, monkeypatch
) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    victim_content = "# stable victim content\n"
    (repo / "victim.py").write_text(victim_content)
    (repo / "doomed.py").write_text("# deleted in the next commit\n")
    _run_git(repo, "add", ".")
    _run_git(repo, "commit", "-m", "add example files")

    metadata_path = tmp_path / "metadata.json"
    first_indexer = _make_smart_indexer(repo, metadata_path)
    first_indexer.smart_index()

    # Establish a real git commit watermark; the first full index does not.
    (repo / "watermark.py").write_text("# establish watermark\n")
    _run_git(repo, "add", ".")
    _run_git(repo, "commit", "-m", "establish watermark")
    _make_smart_indexer(repo, metadata_path).smart_index(safety_buffer_seconds=0)

    collection_name = first_indexer.vector_store_client.resolve_collection_name(
        first_indexer.config, first_indexer.embedding_provider
    )
    collection_path = repo / ".code-indexer" / "index" / collection_name
    assert _vector_files_for_path(collection_path, "victim.py"), (
        "test setup invalid: victim.py has no indexed points"
    )

    # A delete-only commit runs the git-aware incremental finalizer while
    # victim.py remains unchanged. Inject the pre-existing corruption only
    # at end_indexing's final ID-index load, after its HNSW sync, to make
    # the wipe genuinely too late for this run's replay passes.
    (repo / "doomed.py").unlink()
    _run_git(repo, "add", ".")
    _run_git(repo, "commit", "-m", "delete doomed file")
    delete_indexer = _make_smart_indexer(repo, metadata_path)
    store = delete_indexer.vector_store_client
    original_sync = store._resolve_and_publish_hnsw_sync
    original_load = store._load_id_index
    late_wipe = {"injected": False}

    def sync_then_evict(*args, **kwargs):
        result = original_sync(*args, **kwargs)
        # A cold ID-index cache is a valid finalization state. Ensure the
        # finalizer takes its real disk-load path despite earlier deletion
        # work possibly having populated this instance's cache.
        store._id_index.pop(store._id_cache_key(collection_name, None), None)
        return result

    def load_with_late_corruption(
        name: str, subdirectory=None, *, self_heal: bool = False
    ):
        frame = inspect.currentframe()
        caller = frame.f_back if frame is not None else None
        if (
            name == collection_name
            and caller is not None
            and caller.f_code.co_name == "end_indexing"
            and not late_wipe["injected"]
        ):
            assert self_heal is True, "finalization must opt in to repair"
            _duplicate_indexed_record(collection_path, "victim.py")
            _corrupt_id_index_bin(collection_path)
            late_wipe["injected"] = True
        return original_load(name, subdirectory, self_heal=self_heal)

    with monkeypatch.context() as patcher:
        patcher.setattr(store, "_resolve_and_publish_hnsw_sync", sync_then_evict)
        patcher.setattr(store, "_load_id_index", load_with_late_corruption)
        delete_indexer.smart_index(safety_buffer_seconds=0)

    assert late_wipe["injected"], (
        "test setup invalid: the delete-only run never reached the final ID-index load"
    )
    assert not _vector_files_for_path(collection_path, "victim.py"), (
        "test setup invalid: finalization did not wipe victim.py's points"
    )
    assert "victim.py" in store.get_pending_self_heal_reprocess_paths(collection_name)

    # A separate, ordinary incremental invocation has NO new git/mtime
    # change. The durable marker is its only path to restoring coverage.
    next_indexer = _make_smart_indexer(repo, metadata_path)
    next_indexer.smart_index(safety_buffer_seconds=0)
    results = next_indexer.vector_store_client.search(
        query="unused",
        embedding_provider=next_indexer.embedding_provider,
        collection_name=collection_name,
        precomputed_query_vector=_deterministic_embedding(victim_content),
        limit=SEARCH_RESULT_LIMIT,
    )
    assert "victim.py" in {result["payload"].get("path") for result in results}, (
        "the next plain incremental index did not restore the file wiped "
        "during the prior run's end_indexing finalizer"
    )
    assert (
        next_indexer.vector_store_client.get_pending_self_heal_reprocess_paths(
            collection_name
        )
        == frozenset()
    ), "successful replay must retire the pending marker"
