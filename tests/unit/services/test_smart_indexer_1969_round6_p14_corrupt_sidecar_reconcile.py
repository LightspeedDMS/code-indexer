"""Unit test for Bug #1969 Round 6, finding P1-4 (BLOCKING): an unreadable
or malformed self-heal-reprocess marker was silently treated as empty
(`read_pending_self_heal_reprocess_paths` returns `frozenset()` on both a
genuinely absent marker set AND a corrupt one). The vectors are already
gone by the time the marker would have been consulted, so this loses the
only replay record for whatever file it named.

Fix (per the issue's preferred design): a corrupt marker forces that run
into reconcile-with-database mode. `_do_reconcile_with_database`
classifies any file with zero points as missing and reindexes it -- a
complete superset of whatever the lost marker could have listed, since a
self-heal-wiped file has zero points regardless of whether its marker
survives. A loud ERROR names the collection and the corruption. The
corrupt marker is quarantined (moved aside, never deleted outright) only
AFTER that forced reconcile completes successfully, so it stays available
for forensics and a crash mid-reconcile still has the original evidence.

This is a real end-to-end reproduction: a real git repository, a real
`FilesystemVectorStore`, and a real deterministic embedding provider,
driven through the actual `SmartIndexer.smart_index()` production entry
point with a plain incremental call (no `--reconcile` flag) -- no mocking
of the code under test.
"""

import hashlib
import json
import logging
from pathlib import Path

import pytest

from code_indexer.storage.shared.collection_dedup_repair import (
    recover_from_corrupt_id_index_by_wiping_files,
)
from tests.unit.services.test_smart_indexer_1969_round4_incremental_reprocess import (
    _corrupt_id_index_bin,
    _deterministic_embedding,
    _duplicate_indexed_record,
    _init_repo,
    _make_smart_indexer,
    _run_git,
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


def _victim_vector_files(collection_path: Path) -> list[Path]:
    return [
        vector_file
        for vector_file in collection_path.rglob("vector_*.json")
        if json.loads(vector_file.read_text()).get("payload", {}).get("path")
        == "victim.py"
    ]


def test_corrupt_marker_forces_reconcile_and_repairs_missing_file(
    tmp_path: Path, caplog
) -> None:
    """A malformed event marker must not lose a wiped file's replay path."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    victim_content = "# victim with an unreadable replay marker\n"
    (repo / "victim.py").write_text(victim_content)
    (repo / "stable.py").write_text("# stable file\n")
    _run_git(repo, "add", ".")
    _run_git(repo, "commit", "-m", "init")

    metadata_path = tmp_path / "metadata.json"
    first_indexer = _make_smart_indexer(repo, metadata_path)
    first_indexer.smart_index()
    collection_name = first_indexer.vector_store_client.resolve_collection_name(
        first_indexer.config, first_indexer.embedding_provider
    )
    collection_path = repo / ".code-indexer" / "index" / collection_name
    for vector_file in list(collection_path.rglob("vector_*.json")):
        payload = json.loads(vector_file.read_text()).get("payload", {})
        if payload.get("path") == "victim.py":
            vector_file.unlink()

    marker_dir = collection_path / ".self-heal-reprocess-pending.d"
    marker_dir.mkdir(exist_ok=True)
    # Match the production writer's digest-plus-nonce filename without
    # calling it: committed HEAD still uses the old single-file format.
    marker_name = f"{hashlib.sha256(b'victim.py').hexdigest()}.{'0' * 32}"
    corrupt_marker = marker_dir / marker_name
    corrupt_marker.write_text("")

    # The file has no git or mtime change. Only corruption detection can
    # promote this plain incremental run to reconcile and restore coverage.
    next_indexer = _make_smart_indexer(repo, metadata_path)
    with caplog.at_level(logging.ERROR):
        next_indexer.smart_index(safety_buffer_seconds=0)

    assert any(
        record.levelno >= logging.ERROR
        and "1969" in record.getMessage()
        and "P1-4" in record.getMessage()
        for record in caplog.records
    ), "a corrupt individual marker must emit a loud ERROR"
    assert _victim_searchable(next_indexer, collection_name, victim_content), (
        "a corrupt marker must force reconcile so the wiped file becomes searchable"
    )
    assert not corrupt_marker.exists(), (
        "a successful reconcile must retire the bad marker"
    )
    quarantined = list(marker_dir.glob(f"{corrupt_marker.name}.corrupt.*"))
    assert len(quarantined) == 1, "the bad marker must be kept for forensics"
    assert quarantined[0].read_text() == ""


@pytest.mark.parametrize(
    "damaged_content",
    [
        pytest.param(b"victim.p", id="nonempty-wrong-path"),
        pytest.param(b"\xff", id="invalid-utf8"),
    ],
)
def test_damaged_nonempty_marker_forces_reconcile_after_real_wipe(
    tmp_path: Path, caplog, damaged_content: bytes
) -> None:
    """A damaged replay record must restore a genuinely self-heal-wiped file."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _init_repo(repo)
    victim_content = "# unchanged file whose points are wiped\n"
    (repo / "victim.py").write_text(victim_content)
    _run_git(repo, "add", ".")
    _run_git(repo, "commit", "-m", "add example file")

    metadata_path = tmp_path / "metadata.json"
    first_indexer = _make_smart_indexer(repo, metadata_path)
    first_indexer.smart_index()

    # Establish the commit watermark: the next plain incremental run must
    # select the victim only through the replay record, not a git delta.
    (repo / "watermark.py").write_text("# commit watermark\n")
    _run_git(repo, "add", ".")
    _run_git(repo, "commit", "-m", "establish watermark")
    indexed = _make_smart_indexer(repo, metadata_path)
    indexed.smart_index(safety_buffer_seconds=0)
    collection_name = indexed.vector_store_client.resolve_collection_name(
        indexed.config, indexed.embedding_provider
    )
    collection_path = repo / ".code-indexer" / "index" / collection_name
    assert _victim_vector_files(collection_path), "test setup: victim was not indexed"

    # Exercise the actual destructive recovery, including its durable
    # replay record, with a genuine duplicate and corrupt id_index.bin.
    _duplicate_indexed_record(collection_path, "victim.py")
    _corrupt_id_index_bin(collection_path)
    wipe_result = recover_from_corrupt_id_index_by_wiping_files(collection_path)
    assert wipe_result.wiped_relative_paths == frozenset({"victim.py"})
    assert not _victim_vector_files(collection_path), "test setup: wipe did not fire"
    assert indexed.vector_store_client.get_pending_self_heal_reprocess_paths(
        collection_name
    ) == frozenset({"victim.py"})

    # Replace the healthy replay record with a damaged marker. Use the
    # public clear seam so this behavioral test also works against committed
    # HEAD's different, never-shipped sidecar format in the export check.
    indexed.vector_store_client.clear_self_heal_reprocess_paths(
        collection_name, {"victim.py"}
    )
    assert (
        indexed.vector_store_client.get_pending_self_heal_reprocess_paths(
            collection_name
        )
        == frozenset()
    )
    marker_dir = collection_path / ".self-heal-reprocess-pending.d"
    marker_dir.mkdir(exist_ok=True)
    marker_name = f"{hashlib.sha256(b'victim.py').hexdigest()}.{'0' * 32}"
    damaged_marker = marker_dir / marker_name
    damaged_marker.write_bytes(damaged_content)

    # No file or commit changes. An undetected nonempty wrong path leaves
    # victim.py unsearchable; invalid UTF-8 currently crashes this call.
    next_indexer = _make_smart_indexer(repo, metadata_path)
    with caplog.at_level(logging.ERROR):
        next_indexer.smart_index(safety_buffer_seconds=0)

    assert _victim_vector_files(collection_path), (
        "the next plain index did not restore the unchanged wiped file"
    )
    assert _victim_searchable(next_indexer, collection_name, victim_content)
    assert any(
        record.levelno >= logging.ERROR and "P1-4" in record.getMessage()
        for record in caplog.records
    ), "damaged marker must trigger a loud reconcile warning"
    assert not damaged_marker.exists(), "successful reconcile must retire the marker"
    quarantined = list(marker_dir.glob(f"{damaged_marker.name}.corrupt.*"))
    assert len(quarantined) == 1
    assert quarantined[0].read_bytes() == damaged_content
