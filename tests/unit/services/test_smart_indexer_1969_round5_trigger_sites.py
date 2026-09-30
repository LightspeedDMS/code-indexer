"""Unit tests for Bug #1969 Round 5, finding R4-F1 (P2 BLOCKING): the
reviewer traced additional trigger sites where a self-heal wipe fires but
Round 4's in-memory queue never drained it. Each test here reproduces one
of the reviewer's named trigger sites with a REAL git repo, a REAL
`SmartIndexer`/`FilesystemVectorStore`, and a REAL deterministic embedding
provider through the actual `smart_index()` production entry point --
proving the durable sidecar mechanism (recorded once, at the single choke
point `recover_from_corrupt_id_index_by_wiping_files()`) is never
permanently lost, regardless of which code path triggers the wipe.
"""

import pytest

from tests.unit.services.test_smart_indexer_1969_round4_incremental_reprocess import (
    _corrupt_id_index_bin,
    _deterministic_embedding,
    _duplicate_indexed_record,
    _init_repo,
    _make_smart_indexer,
    _run_git,
)
from code_indexer.storage.shared.collection_dedup_repair import (
    read_pending_self_heal_reprocess_paths,
)

SEARCH_RESULT_LIMIT = 10


def _victim_searchable(indexer, collection_name: str, victim_content: str) -> bool:
    results = indexer.vector_store_client.search(
        query="unused",
        embedding_provider=indexer.embedding_provider,
        collection_name=collection_name,
        precomputed_query_vector=_deterministic_embedding(victim_content),
        limit=SEARCH_RESULT_LIMIT,
    )
    return "victim.py" in {r["payload"].get("path") for r in results}


def test_resume_interrupted_reprocesses_self_heal_wipe_same_run(tmp_path):
    """Trigger site: `upsert_points` during `_do_resume_interrupted`.
    The reviewer flags this as the MOST likely real-world trigger, since
    an interrupted run is itself a primary way `id_index.bin` gets
    corrupted in the first place."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    victim_content = "# victim resume content\n"
    (repo / "victim.py").write_text(victim_content)
    (repo / "resume_target.py").write_text("# will be resumed\n")
    _run_git(repo, "add", ".")
    _run_git(repo, "commit", "-m", "init")

    metadata_path = tmp_path / "metadata.json"
    indexer1 = _make_smart_indexer(repo, metadata_path)
    indexer1.smart_index()

    collection_name = indexer1.vector_store_client.resolve_collection_name(
        indexer1.config, indexer1.embedding_provider
    )
    collection_path = repo / ".code-indexer" / "index" / collection_name
    _duplicate_indexed_record(collection_path, "victim.py")
    _corrupt_id_index_bin(collection_path)

    # Simulate an interrupted run: a prior process died mid-index,
    # leaving progressive_metadata in "in_progress" with remaining work.
    pm = indexer1.progressive_metadata
    pm.metadata["status"] = "in_progress"
    pm.metadata["files_to_index"] = [str(repo / "resume_target.py")]
    pm.metadata["current_file_index"] = 0
    pm.metadata["completed_files"] = []
    pm._save_metadata()

    indexer2 = _make_smart_indexer(repo, metadata_path)
    assert indexer2.progressive_metadata.can_resume_interrupted_operation()
    indexer2.smart_index(safety_buffer_seconds=0)

    assert read_pending_self_heal_reprocess_paths(collection_path) == frozenset()
    assert _victim_searchable(indexer2, collection_name, victim_content), (
        "victim.py's self-healed content must be searchable again after "
        "a resumed interrupted run -- the reviewer's flagged most likely "
        "real-world trigger for this corruption."
    )


def test_non_git_detect_deletions_reprocesses_self_heal_wipe(tmp_path):
    """Trigger site: `scroll_points` (unconditional legacy self-heal,
    Bug #1579) inside `_detect_and_handle_deletions`, reached via
    `smart_index(detect_deletions=True)` on a NON-GIT project."""
    repo = tmp_path / "repo"
    repo.mkdir()
    # Deliberately NOT a git repo.
    victim_content = "# victim nongit content\n"
    (repo / "victim.py").write_text(victim_content)
    (repo / "trigger.py").write_text("# trigger file\n")

    metadata_path = tmp_path / "metadata.json"
    indexer1 = _make_smart_indexer(repo, metadata_path)
    indexer1.smart_index()

    collection_name = indexer1.vector_store_client.resolve_collection_name(
        indexer1.config, indexer1.embedding_provider
    )
    collection_path = repo / ".code-indexer" / "index" / collection_name
    _duplicate_indexed_record(collection_path, "victim.py")
    _corrupt_id_index_bin(collection_path)

    # A genuine content change so the non-git incremental mtime scan has
    # something to process.
    (repo / "trigger.py").write_text("# trigger file modified\n")

    indexer2 = _make_smart_indexer(repo, metadata_path)
    indexer2.smart_index(detect_deletions=True, safety_buffer_seconds=0)

    assert read_pending_self_heal_reprocess_paths(collection_path) == frozenset()
    assert _victim_searchable(indexer2, collection_name, victim_content), (
        "victim.py's self-healed content must be searchable again after "
        "a non-git run with detect_deletions=True."
    )


@pytest.mark.timeout(60)
def test_empty_file_commit_wipe_is_not_permanently_lost(tmp_path):
    """Trigger site: `end_indexing`'s own cache-miss self-heal branch,
    fired by an UNRELATED file being emptied to zero content in the same
    commit. Not necessarily resolved in THIS exact run (the wipe can fire
    AFTER this run's own consult points, inside end_indexing's finally
    block) -- but the durable sidecar guarantees it is NEVER permanently
    lost: a subsequent plain rerun picks it up."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    victim_content = "# victim empty-file-test content\n"
    shrink_content = "# will be emptied\n"
    (repo / "victim.py").write_text(victim_content)
    (repo / "shrink.py").write_text(shrink_content)
    _run_git(repo, "add", ".")
    _run_git(repo, "commit", "-m", "init")

    metadata_path = tmp_path / "metadata.json"
    indexer1 = _make_smart_indexer(repo, metadata_path)
    indexer1.smart_index()

    # Priming incremental run to establish a real commit watermark (a
    # fresh full index never records one, which would otherwise mask
    # git-delta-based change detection for the next run).
    (repo / "marker.py").write_text("# marker file to establish watermark\n")
    _run_git(repo, "add", ".")
    _run_git(repo, "commit", "-m", "add marker.py")
    indexer1b = _make_smart_indexer(repo, metadata_path)
    indexer1b.smart_index(safety_buffer_seconds=0)

    collection_name = indexer1.vector_store_client.resolve_collection_name(
        indexer1.config, indexer1.embedding_provider
    )
    collection_path = repo / ".code-indexer" / "index" / collection_name
    _duplicate_indexed_record(collection_path, "victim.py")
    _corrupt_id_index_bin(collection_path)

    # Empty-file commit: shrink.py reduced to zero chunks.
    (repo / "shrink.py").write_text("")
    _run_git(repo, "add", ".")
    _run_git(repo, "commit", "-m", "empty shrink.py")

    indexer2 = _make_smart_indexer(repo, metadata_path)
    indexer2.smart_index(safety_buffer_seconds=0)

    # This exact trigger site is NOT guaranteed same-run (the wipe can
    # fire inside end_indexing's finally, after this run's own consult
    # points already ran) -- what matters is it is durably recorded, not
    # silently lost.
    pending_after_run2 = read_pending_self_heal_reprocess_paths(collection_path)
    assert pending_after_run2 == frozenset({"victim.py"}) or _victim_searchable(
        indexer2, collection_name, victim_content
    ), (
        "victim.py's wipe must be either already resolved this run, or "
        "durably recorded for the next run -- never silently discarded."
    )

    # A subsequent plain rerun (fresh indexer, zero further changes) must
    # pick up the durable record and fully resolve it -- this is the
    # property that actually matters: the wipe is NEVER permanently lost.
    indexer3 = _make_smart_indexer(repo, metadata_path)
    indexer3.smart_index(safety_buffer_seconds=0)

    assert read_pending_self_heal_reprocess_paths(collection_path) == frozenset()
    assert _victim_searchable(indexer3, collection_name, victim_content), (
        "victim.py's self-healed content must be searchable again after "
        "the durable sidecar was consulted on a later plain rerun -- "
        "proving the empty-file-commit trigger site never causes "
        "PERMANENT loss, even when it is not resolved same-run."
    )
