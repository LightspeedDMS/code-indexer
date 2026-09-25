"""Unit test for Bug #1969 Round 5, finding R4-F2 (P3): a deleted file is
not actually hidden/removed from the index when an UNRELATED file's
self-heal fires in the same run.

Root cause (confirmed via a live traceback capture during investigation):
`delete_file_branch_aware()` -> `_hide_file_in_branch_thread_safe()` ->
`FilesystemVectorStore.scroll_points()` -> `get_point()`, and separately
`hide_files_not_in_branch_thread_safe()` -> `_fetch_points_to_hide()` ->
`fetch_points_for_paths()` -> `get_point()`. `get_point()` defaults to
`self_heal=False` (correctly, since it is called from many genuinely
read-only contexts) and calls `_load_id_index(..., self_heal=False)` --
when `id_index.bin` is corrupt AND a genuine duplicate exists elsewhere in
the collection, this raises `DuplicateSourceIdError` uncaught, which both
call sites silently swallow (log + return False/None), so the deleted
file's records are NEVER actually removed from the index this run. Only
LATER in the same run, when `end_indexing()`'s own `_load_id_index(
self_heal=True)` call finally self-heals the corruption, does the
underlying problem get fixed -- too late for this run's own delete/hide
attempts, which already gave up.

This is a REAL end-to-end reproduction: a real git repository, a real
`FilesystemVectorStore`, and a real deterministic embedding provider,
through the actual `smart_index()` production entry point. Must fail
(the deleted file's content still searchable) before the fix, pass
(deleted file's content gone) after.
"""

import pytest

pytest.importorskip(
    "tests.unit.services.test_smart_indexer_1969_round4_incremental_reprocess"
)

from tests.unit.services.test_smart_indexer_1969_round4_incremental_reprocess import (  # noqa: E402
    _corrupt_id_index_bin,
    _deterministic_embedding,
    _duplicate_indexed_record,
    _init_repo,
    _make_smart_indexer,
    _run_git,
)

SEARCH_RESULT_LIMIT = 10


def test_delete_only_commit_deletes_file_despite_unrelated_self_heal_same_run(
    tmp_path,
):
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)

    victim_content = "# victim file content untouched across runs\n"
    doomed_content = "# doomed file, will be deleted\n"
    (repo / "victim.py").write_text(victim_content)
    (repo / "doomed.py").write_text(doomed_content)
    _run_git(repo, "add", ".")
    _run_git(repo, "commit", "-m", "initial: add victim.py and doomed.py")

    metadata_path = tmp_path / "metadata.json"

    # Run 1: real full index.
    indexer1 = _make_smart_indexer(repo, metadata_path)
    indexer1.smart_index()

    # Priming incremental run: establishes a real commit watermark so
    # run 2's git-delta deletion detection actually fires (a fresh
    # `_do_full_index()` never records one).
    (repo / "marker.py").write_text("# marker file to establish watermark\n")
    _run_git(repo, "add", ".")
    _run_git(repo, "commit", "-m", "add marker.py")
    indexer1b = _make_smart_indexer(repo, metadata_path)
    indexer1b.smart_index(safety_buffer_seconds=0)

    collection_name = indexer1.vector_store_client.resolve_collection_name(
        indexer1.config, indexer1.embedding_provider
    )
    collection_path = repo / ".code-indexer" / "index" / collection_name

    # Simulate the pre-existing Bug #1969 corruption on victim.py.
    _duplicate_indexed_record(collection_path, "victim.py")
    _corrupt_id_index_bin(collection_path)

    # Delete-only commit: doomed.py removed from git, nothing else
    # changes.
    (repo / "doomed.py").unlink()
    _run_git(repo, "add", ".")
    _run_git(repo, "commit", "-m", "delete doomed.py")

    indexer2 = _make_smart_indexer(repo, metadata_path)
    indexer2.smart_index(safety_buffer_seconds=0)

    doomed_vector = _deterministic_embedding(doomed_content)
    results = indexer2.vector_store_client.search(
        query="unused",
        embedding_provider=indexer2.embedding_provider,
        collection_name=collection_name,
        precomputed_query_vector=doomed_vector,
        limit=SEARCH_RESULT_LIMIT,
    )
    result_paths = {r["payload"].get("path") for r in results}
    assert "doomed.py" not in result_paths, (
        "doomed.py was deleted via git but its content is STILL "
        "searchable after the same run that also self-healed an "
        "UNRELATED file's corruption -- the delete/hide path silently "
        f"gave up instead of self-healing. Got paths: {result_paths}"
    )

    # Beneficial side effect of the R4-F2 fix: the delete/hide self-heal
    # now fires EARLIER in the run (during git-delta deletion handling,
    # before _do_incremental_index's upfront R4-F1 fold-in step), so
    # victim.py's wipe is durably recorded in time for the SAME run's
    # primary pass to pick it up -- no need to wait for a later run.
    victim_vector = _deterministic_embedding(victim_content)
    victim_results = indexer2.vector_store_client.search(
        query="unused",
        embedding_provider=indexer2.embedding_provider,
        collection_name=collection_name,
        precomputed_query_vector=victim_vector,
        limit=SEARCH_RESULT_LIMIT,
    )
    victim_paths = {r["payload"].get("path") for r in victim_results}
    assert "victim.py" in victim_paths, (
        "victim.py's self-healed content should also be searchable again "
        f"within this same run. Got paths: {victim_paths}"
    )
