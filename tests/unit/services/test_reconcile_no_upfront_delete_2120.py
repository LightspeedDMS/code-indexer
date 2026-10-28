"""Story #2120: rows a reuse lookup could use are never removed before the run
has processed the files that might reuse them, and a populated collection is
never cleared implicitly.

Every test drives the REAL ``cidx`` CLI (subprocess, the tree under test) in a
temporary non-git repository (the ``local://`` shape) against the local fake
VoyageAI server (plain HTTP on 127.0.0.1, sentinel key): no real provider call
is possible. The fake records the sha256 of every input text, which equals the
``content_hash`` the indexer stores per chunk.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Set

import pytest

from tests.utils.cidx_fake_provider_repo import (
    FakeProviderRepo,
    kill_after_rows,
    kill_at_first_held_request,
    unique_python_text,
)
from tests.utils.fake_voyage_provider import running_fake_voyage_server

_SENTINEL_KEY = "issue-2120-fake-key"

pytestmark = [pytest.mark.slow, pytest.mark.timeout(240)]


@pytest.fixture
def fake_provider() -> Iterator[object]:
    # The shared helper stops the fake and asserts no ledger violation.
    with running_fake_voyage_server(_SENTINEL_KEY) as server:
        yield server


def _indexed_repo(tmp_path: Path, server) -> FakeProviderRepo:
    repo = FakeProviderRepo(tmp_path, server, _SENTINEL_KEY)
    repo.write_files("base", 6)
    server.ledger.begin_run("initial")
    proc = repo.cidx(*repo.index_args())
    assert proc.returncode == 0, proc.stderr[-3000:]
    assert server.ledger.run_stats("initial")["inputs"] > 0
    return repo


def _delete_metadata(repo: FakeProviderRepo) -> None:
    repo.metadata_path.unlink()


def _corrupt_metadata(repo: FakeProviderRepo) -> None:
    repo.metadata_path.write_text("{ this is not json")


def _edit_metadata(**fields: object) -> Callable[[FakeProviderRepo], None]:
    def edit(repo: FakeProviderRepo) -> None:
        metadata: Dict[str, object] = repo.metadata()
        metadata.update(fields)
        repo.metadata_path.write_text(json.dumps(metadata))

    return edit


@pytest.mark.parametrize(
    "damage_metadata",
    [
        pytest.param(_delete_metadata, id="metadata-deleted"),
        pytest.param(_corrupt_metadata, id="metadata-invalid-json"),
        pytest.param(_edit_metadata(git_available=True), id="git-available-flipped"),
        pytest.param(_edit_metadata(project_id="another-project"), id="project-drift"),
        # Matching configuration, but no resume watermark: the incremental
        # path's ``resume_timestamp == 0`` route.
        pytest.param(_edit_metadata(status="not_started"), id="status-not-started"),
        pytest.param(_edit_metadata(last_index_timestamp=0.0), id="watermark-zero"),
    ],
)
def test_metadata_mismatch_on_a_populated_collection_reconciles_without_wipe(
    tmp_path: Path, fake_provider, damage_metadata
) -> None:
    """Missing or corrupt metadata, a ``git_available`` flip or ``project_id``
    drift once wiped the populated collection (``_do_full_index`` +
    ``clear_collection``) and paid for every chunk again. Now the run
    reconciles the store against disk: nothing is planned, nothing is sent,
    the rows are untouched, and the metadata is rewritten so the next run is
    a normal incremental one."""
    repo = _indexed_repo(tmp_path, fake_provider)
    rows_before = repo.stored_rows()
    damage_metadata(repo)

    fake_provider.ledger.begin_run("after-damage")
    damaged_run = repo.cidx(*repo.index_args())

    assert damaged_run.returncode == 0, damaged_run.stderr[-3000:]
    assert fake_provider.ledger.run_stats("after-damage")["inputs"] == 0, (
        "the populated collection was rebuilt and the provider paid again"
    )
    assert "Existing index kept" in damaged_run.stdout, damaged_run.stdout[-2000:]
    assert repo.stored_rows() == rows_before
    metadata = repo.metadata()
    assert metadata["status"] == "completed"
    assert metadata["files_processed"] == 0, "the reconcile planned files"
    assert metadata["embedding_provider"] == "voyage-ai"
    assert metadata["git_available"] is False

    # The metadata was rewritten: the next run is a plain incremental one
    # (files inside its 60 s safety buffer are re-planned and reused).
    fake_provider.ledger.begin_run("next")
    next_run = repo.cidx(*repo.index_args())
    assert next_run.returncode == 0, next_run.stderr[-3000:]
    assert fake_provider.ledger.run_stats("next")["inputs"] == 0
    assert "Existing index kept" not in next_run.stdout, next_run.stdout[-2000:]


def test_explicit_clear_still_rebuilds_from_scratch(
    tmp_path: Path, fake_provider
) -> None:
    """``--clear`` stays the one route that wipes a populated collection: it
    re-embeds every chunk (a full paid rebuild, by request)."""
    repo = _indexed_repo(tmp_path, fake_provider)
    stored_before = repo.stored_content_hashes()

    fake_provider.ledger.begin_run("clear")
    proc = repo.cidx(*repo.index_args(), "--clear")

    assert proc.returncode == 0, proc.stderr[-3000:]
    assert fake_provider.ledger.run_hashes("clear") == stored_before
    assert repo.stored_content_hashes() == stored_before


@pytest.mark.parametrize(
    "damage_metadata",
    [
        pytest.param(_delete_metadata, id="metadata-deleted"),
        pytest.param(_corrupt_metadata, id="metadata-invalid-json"),
    ],
)
def test_interrupted_first_build_with_lost_metadata_is_not_wiped(
    tmp_path: Path, fake_provider, damage_metadata
) -> None:
    """Codex P1-2: "populated" is decided from the committed store itself.
    A FIRST build killed after some rows committed has published no
    collection count yet; with its metadata lost, the rerun must reconcile
    (reusing every committed row), never clear the collection and pay."""
    repo = FakeProviderRepo(tmp_path, fake_provider, _SENTINEL_KEY)
    repo.write_files("first", 40)
    fake_provider.ledger.begin_run("killed-first-build", hold_seconds=0.4)
    kill_after_rows(repo, 15, deadline_s=90)
    committed = repo.stored_content_hashes()
    assert len(committed) >= 15
    damage_metadata(repo)

    fake_provider.ledger.begin_run("rerun")
    proc = repo.cidx(*repo.index_args())

    assert proc.returncode == 0, proc.stderr[-3000:]
    resent = fake_provider.ledger.run_hashes("rerun") & committed
    assert resent == set(), (
        f"{len(resent)} chunk(s) committed by the interrupted first build were "
        f"paid again: the collection was cleared"
    )
    assert committed <= repo.stored_content_hashes()


def test_stale_zero_published_count_does_not_hide_committed_rows(
    tmp_path: Path, fake_provider
) -> None:
    """Codex P1-2: the published collection count (collection_meta.json)
    can lag the committed store; a stale 0 there must not make a populated
    collection look empty and get it cleared when the metadata is lost."""
    repo = _indexed_repo(tmp_path, fake_provider)
    rows_before = repo.stored_rows()
    meta_path = repo.collection_dir / "collection_meta.json"
    meta = json.loads(meta_path.read_text())
    meta.setdefault("hnsw_index", {})["vector_count"] = 0
    meta_path.write_text(json.dumps(meta))
    _delete_metadata(repo)

    fake_provider.ledger.begin_run("rerun")
    proc = repo.cidx(*repo.index_args())

    assert proc.returncode == 0, proc.stderr[-3000:]
    assert fake_provider.ledger.run_stats("rerun")["inputs"] == 0, (
        "a stale published count of 0 hid the committed rows and the "
        "collection was rebuilt"
    )
    assert repo.stored_rows() == rows_before


def _chunk_keys(repo: FakeProviderRepo, path: Path) -> List[str]:
    """sha256 of each chunk text (the indexer's content_hash), from the
    tree's chunker configured by the repository's own config."""
    from code_indexer.config import ConfigManager
    from code_indexer.indexing.fixed_size_chunker import FixedSizeChunker

    chunker = FixedSizeChunker(
        ConfigManager(repo.repo / ".code-indexer" / "config.json").load()
    )
    return [
        hashlib.sha256(chunk["text"].encode("utf-8")).hexdigest()
        for chunk in chunker.chunk_file(path, repo.repo)
    ]


def _stored_hashes_at(repo: FakeProviderRepo, relative_path: str) -> Set[str]:
    """content_hash of every committed row stored under ``relative_path``."""
    from code_indexer.storage.sqlite_chunk_store import ChunkStore

    with ChunkStore(repo.chunks_db) as store:
        return {
            record["payload"]["content_hash"]
            for record in store.stream_all()
            if record.get("payload", {}).get("path") == relative_path
        }


def test_modified_file_killed_after_plan_resends_only_changed_chunks(
    tmp_path: Path, fake_provider
) -> None:
    """The non-git reconcile once deleted every row of a modified file
    BEFORE processing it. A run killed after its plan therefore left the
    file with no rows, and the recovery paid for every chunk again. Now the
    old rows stay until the file's replacement is written, so the recovery
    sends only the chunks that really changed."""
    repo = _indexed_repo(tmp_path, fake_provider)
    target = repo.repo / "src" / "long_module.py"
    target.write_text(unique_python_text("long_module", 200))
    fake_provider.ledger.begin_run("long-module")
    proc = repo.cidx(*repo.index_args())
    assert proc.returncode == 0, proc.stderr[-3000:]
    old_keys = set(_chunk_keys(repo, target))
    assert _stored_hashes_at(repo, "src/long_module.py") == old_keys
    target.write_text(target.read_text() + unique_python_text("appended", 12))
    new_keys = set(_chunk_keys(repo, target))
    changed = new_keys - old_keys
    assert changed and new_keys & old_keys, "fixture: some chunks must survive"

    fake_provider.ledger.begin_run("killed", hold_seconds=60)
    kill_at_first_held_request(repo, "--reconcile")

    fake_provider.ledger.begin_run("recovery")
    proc = repo.cidx(*repo.index_args(), "--reconcile")

    assert proc.returncode == 0, proc.stderr[-3000:]
    sent = fake_provider.ledger.run_hashes("recovery")
    assert sent == changed, (
        f"the recovery sent {len(sent - changed)} unchanged chunk(s) of the "
        f"modified file again (expected only its {len(changed)} changed one(s))"
    )
    assert _stored_hashes_at(repo, "src/long_module.py") == new_keys


def test_replacing_a_file_removes_rows_an_interrupted_run_left_outside_the_path_index(
    tmp_path: Path, fake_provider
) -> None:
    """A run killed before ``end_indexing`` commits a file's new rows to
    chunks.db but never persists ``path_index.bin``. When the file changes
    again, its replacement must remove those rows too: the old ids of a file
    are read from chunks.db by path, not from the (stale) path index.
    The killed run's on-disk state is reproduced exactly: v2 is indexed,
    then the path index persisted for v1 is put back."""
    repo = _indexed_repo(tmp_path, fake_provider)
    target = repo.repo / "src" / "long_module.py"
    target.write_text(unique_python_text("long_module", 200))
    fake_provider.ledger.begin_run("v1")
    assert repo.cidx(*repo.index_args()).returncode == 0
    path_index = repo.collection_dir / "path_index.bin"
    v1_path_index = path_index.read_bytes()

    target.write_text(unique_python_text("long_module_v2", 200))
    fake_provider.ledger.begin_run("v2")
    assert repo.cidx(*repo.index_args()).returncode == 0
    v2_keys = set(_chunk_keys(repo, target))
    assert _stored_hashes_at(repo, "src/long_module.py") == v2_keys
    path_index.write_bytes(v1_path_index)

    target.write_text(unique_python_text("long_module_v3", 200))
    v3_keys = set(_chunk_keys(repo, target))
    fake_provider.ledger.begin_run("v3")
    proc = repo.cidx(*repo.index_args(), "--reconcile")

    assert proc.returncode == 0, proc.stderr[-3000:]
    leftover = _stored_hashes_at(repo, "src/long_module.py") - v3_keys
    assert leftover == set(), (
        f"{len(leftover)} row(s) of the interrupted run's version stayed "
        f"stored next to the file's current content"
    )
    assert _stored_hashes_at(repo, "src/long_module.py") == v3_keys


def test_deleting_the_stored_owner_of_a_duplicate_group_sends_nothing(
    tmp_path: Path, fake_provider
) -> None:
    """Byte-identical copies share content-addressed point ids, so the
    group's rows carry ONE copy's path (the stored owner). The reconcile
    once deleted a gone path's rows BEFORE processing: deleting the owner
    from disk destroyed the rows the live copies reuse, and they were paid
    again. Now gone paths are removed after processing, and only rows still
    stored under the gone path: the copies reuse the rows (taking them
    over), nothing is sent, and a plain gone file is still removed."""
    repo = FakeProviderRepo(tmp_path, fake_provider, _SENTINEL_KEY)
    repo.write_files("base", 4)
    shared = unique_python_text("shared_group", 120)
    for copy in range(4):
        (repo.repo / "src" / f"dup_{copy}.py").write_text(shared)
    fake_provider.ledger.begin_run("initial")
    assert repo.cidx(*repo.index_args()).returncode == 0

    group_keys = set(_chunk_keys(repo, repo.repo / "src" / "dup_0.py"))
    owners = {
        path for path in repo.stored_rows().values() if path.startswith("src/dup_")
    }
    assert len(owners) == 1, f"fixture: the group's rows carry one path {owners}"
    owner = owners.pop()
    (repo.repo / owner).unlink()
    (repo.repo / "src" / "base_000.py").unlink()

    fake_provider.ledger.begin_run("after-delete")
    proc = repo.cidx(*repo.index_args(), "--reconcile")

    assert proc.returncode == 0, proc.stderr[-3000:]
    assert fake_provider.ledger.run_stats("after-delete")["inputs"] == 0, (
        "the duplicate group's rows were deleted with their gone owner path "
        "and the live copies paid again"
    )
    stored_paths = set(repo.stored_rows().values())
    assert owner not in stored_paths
    assert "src/base_000.py" not in stored_paths, "a gone file kept its rows"
    live_copy_hashes = set().union(
        *(
            _stored_hashes_at(repo, f"src/dup_{copy}.py")
            for copy in range(4)
            if f"src/dup_{copy}.py" != owner
        )
    )
    assert live_copy_hashes == group_keys


def _rows_by_chunk_index(repo: FakeProviderRepo, relative_path: str) -> Dict[int, Any]:
    """chunk_index -> (point_id, content_hash, text) of the rows stored
    under ``relative_path``."""
    from code_indexer.storage.sqlite_chunk_store import ChunkStore

    with ChunkStore(repo.chunks_db) as store:
        return {
            record["payload"]["chunk_index"]: (
                record["id"],
                record["payload"]["content_hash"],
                record.get("chunk_text") or record["payload"].get("content"),
            )
            for record in store.stream_all()
            if record.get("payload", {}).get("path") == relative_path
        }


def _point_provider_at(repo: FakeProviderRepo, endpoint: str) -> None:
    config_path = repo.repo / ".code-indexer" / "config.json"
    config = json.loads(config_path.read_text())
    config["voyage_ai"]["api_endpoint"] = endpoint
    config["voyage_ai"]["max_retries"] = 0
    config_path.write_text(json.dumps(config))


def test_failed_live_copy_keeps_the_shared_rows_of_its_gone_twin(
    tmp_path: Path, fake_provider
) -> None:
    """Codex P1-1: the deferred gone-path delete must not remove rows a live
    byte-identical copy still owes when that copy FAILED in this run (here
    the shared rows are partial, as an interrupted run leaves them, so the
    copy needs the provider, which is down). The rows survive and stay
    queryable, the failure is recorded, the store is not marked verified,
    and the next run completes the copy paying only for what is missing."""
    repo = FakeProviderRepo(tmp_path, fake_provider, _SENTINEL_KEY)
    repo.write_files("base", 2)
    shared = unique_python_text("shared_group", 160)
    for copy in range(2):
        (repo.repo / "src" / f"dup_{copy}.py").write_text(shared)
    fake_provider.ledger.begin_run("initial")
    assert repo.cidx(*repo.index_args()).returncode == 0
    owners = {p for p in repo.stored_rows().values() if p.startswith("src/dup_")}
    assert len(owners) == 1
    owner = owners.pop()
    live_copy = "src/dup_1.py" if owner == "src/dup_0.py" else "src/dup_0.py"
    rows = _rows_by_chunk_index(repo, owner)
    assert len(rows) >= 5, "fixture: the shared content needs several chunks"
    dropped = sorted(rows)[-3:]
    conn = sqlite3.connect(str(repo.chunks_db))
    try:
        conn.executemany(
            "DELETE FROM chunks WHERE point_id = ?", [(rows[i][0],) for i in dropped]
        )
        conn.commit()
    finally:
        conn.close()
    surviving = {rows[i][1] for i in rows if i not in dropped}
    (repo.repo / owner).unlink()
    _point_provider_at(repo, "http://127.0.0.1:9/v1/embeddings")

    proc = repo.cidx(*repo.index_args(), "--reconcile")

    # The only planned file failed: the run fails loudly (it used to report
    # success while the failure was dropped).
    assert proc.returncode == 1, (proc.returncode, proc.stdout[-2000:])
    assert "All files failed" in proc.stdout, proc.stdout[-2000:]
    stored = repo.stored_content_hashes()
    assert surviving <= stored, (
        f"{len(surviving - stored)} shared row(s) the failed live copy still "
        f"owes were deleted with its gone twin"
    )
    metadata = repo.metadata()
    failed_paths = metadata["failed_file_paths"]
    assert isinstance(failed_paths, list)
    assert str(repo.repo / live_copy) in failed_paths
    assert metadata["store_verified_by_reconcile"] is False

    _point_provider_at(repo, f"{fake_provider.base_url}/v1/embeddings")
    query = repo.query(rows[sorted(rows)[0]][2], "--limit", "3")
    assert query.returncode == 0, query.stderr[-2000:]
    assert "dup_" in query.stdout, query.stdout[-2000:]

    fake_provider.ledger.begin_run("recovery")
    proc = repo.cidx(*repo.index_args(), "--reconcile")

    assert proc.returncode == 0, proc.stderr[-3000:]
    assert fake_provider.ledger.run_hashes("recovery") == {rows[i][1] for i in dropped}
    assert _stored_hashes_at(repo, live_copy) == {h for _, h, _ in rows.values()}


@pytest.mark.parametrize(
    "route", [["--detect-deletions"], ["--reconcile"]], ids=["detect", "reconcile"]
)
def test_deleted_file_is_not_returned_by_fts_or_semantic_queries(
    tmp_path: Path, fake_provider, route: List[str]
) -> None:
    """Codex P1-3: after a run that removed a deleted file, neither a
    full-text nor a semantic query returns it (the vector rows, the FTS
    document and the HNSW entries all go), while kept files are found."""
    repo = FakeProviderRepo(tmp_path, fake_provider, _SENTINEL_KEY)
    repo.write_files("kept", 2)
    gone = repo.repo / "src" / "gone_module.py"
    gone.write_text(
        "def zebracornword_handler(value):\n    return value * 7\n"
        + unique_python_text("gone_module", 6)
    )
    fake_provider.ledger.begin_run("initial")
    proc = repo.cidx(*repo.index_args(), "--fts")
    assert proc.returncode == 0, proc.stderr[-3000:]
    gone_text = _rows_by_chunk_index(repo, "src/gone_module.py")[0][2]
    kept_text = _rows_by_chunk_index(repo, "src/kept_000.py")[0][2]
    gone.unlink()

    proc = repo.cidx(*repo.index_args(), "--fts", *route)
    assert proc.returncode == 0, proc.stderr[-3000:]

    fts = repo.query("zebracornword_handler", "--fts")
    assert fts.returncode == 0, fts.stderr[-2000:]
    assert "gone_module" not in fts.stdout, fts.stdout[-2000:]
    semantic = repo.query(gone_text, "--limit", "5")
    assert semantic.returncode == 0, semantic.stderr[-2000:]
    assert "gone_module" not in semantic.stdout, semantic.stdout[-2000:]
    kept = repo.query(kept_text, "--limit", "5")
    assert "kept_000" in kept.stdout, kept.stdout[-2000:]


def _fts_fault_injection(flag: Path, mode: str) -> str:
    """``sitecustomize`` code (appended to the guarded one) that, only while
    ``flag`` exists, makes the child's FTS deletes fail: ``raise`` -> the
    deferred delete raises; ``legacy`` -> the index reports it cannot delete
    by path (a legacy pre-#1761 schema)."""
    return (
        "import os\n"
        f"if os.path.exists({str(flag)!r}):\n"
        "    import code_indexer.services.tantivy_index_manager as _tim\n"
        f"    if {mode!r} == 'raise':\n"
        "        _original_delete = _tim.TantivyIndexManager.delete_document_deferred\n"
        "        def _failing_delete(self, file_path):\n"
        "            if 'gone_module' in str(file_path):\n"
        "                raise OSError('injected FTS delete failure')\n"
        "            return _original_delete(self, file_path)\n"
        "        _tim.TantivyIndexManager.delete_document_deferred = _failing_delete\n"
        "    else:\n"
        "        _tim.TantivyIndexManager._path_exact_field_available = (\n"
        "            lambda self: False\n"
        "        )\n"
    )


@pytest.mark.parametrize(
    "mode, route",
    [("raise", ["--detect-deletions"]), ("legacy", ["--reconcile"])],
    ids=["delete-raises-detect", "legacy-index-reconcile"],
)
def test_unresolved_fts_delete_leaves_the_index_to_be_rebuilt(
    tmp_path: Path, fake_provider, mode: str, route: List[str]
) -> None:
    """Codex r2 P2-1: a gone path whose FTS document could not be deleted
    (the delete failed, or the index cannot delete by path) must not leave
    a complete-looking FTS index: the run leaves it without its content
    marker, so the next run rebuilds it from disk and the deleted file is
    no longer returned."""
    from code_indexer.services.fts_lifecycle import fts_content_rebuild_due

    flag = tmp_path / "inject-fts-fault"
    repo = FakeProviderRepo(
        tmp_path,
        fake_provider,
        _SENTINEL_KEY,
        extra_sitecustomize=_fts_fault_injection(flag, mode),
    )
    repo.write_files("kept", 2)
    gone = repo.repo / "src" / "gone_module.py"
    gone.write_text("def zebracornword_handler(value):\n    return value * 7\n")
    fake_provider.ledger.begin_run("initial")
    assert repo.cidx(*repo.index_args(), "--fts").returncode == 0
    gone.unlink()

    flag.write_text("on")
    proc = repo.cidx(*repo.index_args(), "--fts", *route)
    flag.unlink()

    assert proc.returncode == 0, proc.stderr[-3000:]
    assert fts_content_rebuild_due(repo.repo), (
        "an unresolved FTS delete left the index marked complete"
    )

    proc = repo.cidx(*repo.index_args(), "--fts")
    assert proc.returncode == 0, proc.stderr[-3000:]
    fts = repo.query("zebracornword_handler", "--fts")
    assert fts.returncode == 0, fts.stderr[-2000:]
    assert "gone_module" not in fts.stdout, fts.stdout[-2000:]


def _path_index_persist_hook(log_path: Path) -> str:
    """``sitecustomize`` code for the cidx child (appended to the repo
    helper's guarded one): a read-only PEP 578 audit hook that appends the
    byte size of every file atomically renamed onto a ``path_index.bin``
    (``os.replace`` raises the ``os.rename`` event) to ``log_path``. Nothing
    in the indexer changes."""
    return (
        "import os\n"
        f"_PERSIST_LOG = {str(log_path)!r}\n"
        "def _persist_hook(event, args):\n"
        "    if event == 'os.rename' and str(args[1]).endswith('path_index.bin'):\n"
        "        with open(_PERSIST_LOG, 'a') as log:\n"
        "            log.write(f'{os.path.getsize(args[0])}\\n')\n"
        "sys.addaudithook(_persist_hook)\n"
    )


def test_mass_deletion_persists_the_path_index_a_bounded_number_of_times(
    tmp_path: Path, fake_provider
) -> None:
    """Bug #2124: removing N gone files rewrote the whole path_index.bin once
    per file (O(N x index size) bytes; 523 GB for ~199k files on staging).
    The reconcile now removes them in one batch: the path index is persisted
    a bounded number of times whatever N is."""
    # The real CLI indexes about 10 files/s here (one request per file), so
    # the setup stays small; the bound below does not depend on the count.
    deleted_count = 150
    kept_count = 5
    max_path_index_persists = 2
    reconcile_time_budget_s = 30
    persist_log = tmp_path / "path_index_persists.log"
    repo = FakeProviderRepo(
        tmp_path,
        fake_provider,
        _SENTINEL_KEY,
        extra_sitecustomize=_path_index_persist_hook(persist_log),
    )
    repo.write_files("bulk", deleted_count + kept_count, lines=2)
    fake_provider.ledger.begin_run("initial")
    proc = repo.cidx(*repo.index_args())
    assert proc.returncode == 0, proc.stderr[-3000:]
    for i in range(deleted_count):
        (repo.repo / "src" / f"bulk_{i:03d}.py").unlink()
    persist_log.write_text("")

    fake_provider.ledger.begin_run("mass-delete")
    started = time.monotonic()
    proc = repo.cidx(*repo.index_args(), "--reconcile")
    elapsed = time.monotonic() - started

    assert proc.returncode == 0, proc.stderr[-3000:]
    persist_sizes = [int(line) for line in persist_log.read_text().split()]
    assert len(persist_sizes) <= max_path_index_persists, (
        f"path_index.bin was persisted {len(persist_sizes)} times "
        f"(limit {max_path_index_persists}, {sum(persist_sizes)} bytes) "
        f"to remove {deleted_count} files"
    )
    assert fake_provider.ledger.run_stats("mass-delete")["inputs"] == 0
    assert set(repo.stored_rows().values()) == {
        f"src/bulk_{i:03d}.py" for i in range(deleted_count, deleted_count + kept_count)
    }
    assert elapsed < reconcile_time_budget_s, (
        f"removing {deleted_count} files took {elapsed:.1f} s "
        f"(budget {reconcile_time_budget_s} s)"
    )


def test_detect_deletions_persists_the_path_index_a_bounded_number_of_times(
    tmp_path: Path, fake_provider
) -> None:
    """Bug #2124 on the CLI incremental path: ``--detect-deletions`` removed
    gone files one by one, persisting path_index.bin per file. It now
    removes them in one batch with one persist."""
    deleted_count = 150
    kept_count = 5
    max_path_index_persists = 2
    persist_log = tmp_path / "path_index_persists.log"
    repo = FakeProviderRepo(
        tmp_path,
        fake_provider,
        _SENTINEL_KEY,
        extra_sitecustomize=_path_index_persist_hook(persist_log),
    )
    repo.write_files("bulk", deleted_count + kept_count, lines=2)
    fake_provider.ledger.begin_run("initial")
    assert repo.cidx(*repo.index_args()).returncode == 0
    for i in range(deleted_count):
        (repo.repo / "src" / f"bulk_{i:03d}.py").unlink()
    persist_log.write_text("")

    fake_provider.ledger.begin_run("detect-deletions")
    proc = repo.cidx(*repo.index_args(), "--detect-deletions")

    assert proc.returncode == 0, proc.stderr[-3000:]
    persist_sizes = [int(line) for line in persist_log.read_text().split()]
    assert len(persist_sizes) <= max_path_index_persists, (
        f"path_index.bin was persisted {len(persist_sizes)} times "
        f"(limit {max_path_index_persists}, {sum(persist_sizes)} bytes) "
        f"to remove {deleted_count} files"
    )
    assert fake_provider.ledger.run_stats("detect-deletions")["inputs"] == 0
    assert set(repo.stored_rows().values()) == {
        f"src/bulk_{i:03d}.py" for i in range(deleted_count, deleted_count + kept_count)
    }


def test_detect_deletions_keeps_a_duplicate_owners_rows_for_its_live_copy(
    tmp_path: Path, fake_provider
) -> None:
    """``--detect-deletions`` once deleted a gone path's rows BEFORE the
    incremental processed its files: deleting a duplicate group's stored
    owner destroyed the rows a re-planned live copy reuses, and it paid.
    The gone path is now removed after processing, and only rows still
    stored under it."""
    repo = FakeProviderRepo(tmp_path, fake_provider, _SENTINEL_KEY)
    repo.write_files("base", 2)
    shared = unique_python_text("shared_group", 120)
    for copy in range(3):
        (repo.repo / "src" / f"dup_{copy}.py").write_text(shared)
    fake_provider.ledger.begin_run("initial")
    assert repo.cidx(*repo.index_args()).returncode == 0
    group_keys = set(_chunk_keys(repo, repo.repo / "src" / "dup_0.py"))
    owners = {
        path for path in repo.stored_rows().values() if path.startswith("src/dup_")
    }
    assert len(owners) == 1, f"fixture: the group's rows carry one path {owners}"
    owner = owners.pop()
    (repo.repo / owner).unlink()
    live_copy = next(
        f"src/dup_{copy}.py" for copy in range(3) if f"src/dup_{copy}.py" != owner
    )
    future = time.time() + 120
    os.utime(repo.repo / live_copy, (future, future))

    fake_provider.ledger.begin_run("detect-deletions")
    proc = repo.cidx(*repo.index_args(), "--detect-deletions")

    assert proc.returncode == 0, proc.stderr[-3000:]
    assert fake_provider.ledger.run_stats("detect-deletions")["inputs"] == 0, (
        "the duplicate group's rows were deleted with their gone owner path "
        "before the live copy could reuse them"
    )
    assert owner not in set(repo.stored_rows().values())
    # Whichever live copy was upserted last owns the shared rows.
    live_copy_hashes = set().union(
        *(
            _stored_hashes_at(repo, f"src/dup_{copy}.py")
            for copy in range(3)
            if f"src/dup_{copy}.py" != owner
        )
    )
    assert live_copy_hashes == group_keys
