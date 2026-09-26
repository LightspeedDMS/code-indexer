"""Unit tests for Bug #1969 Round 6, finding P1-1's caller audit: once
`scroll_points()`'s destructive repair/wipe escalation is gated behind
`self_heal` (defaulting to False), every LEGITIMATE write/reconcile-ish
caller that relied on the old unconditional self-heal must explicitly opt
in with `self_heal=True`, or it regresses to hard-failing (or silently
masking a failure) on a pre-existing corrupt-duplicate collection instead
of self-healing it.

This module covers four indexing call sites, the live reconcile
visibility-cleanup scan, and branch cleanup. Other store and service
callers require separate classification; a call in indexing code is not
automatically safe to mutate.

1. `FilesystemVectorStore.delete_by_filter` (filesystem_vector_store.py)
2. `HighThroughputProcessor._fetch_all_content_points`
   (high_throughput_processor.py)
3. `SmartIndexer._scroll_all_content_points` (smart_indexer.py)
4. `SmartIndexer._detect_and_handle_deletions` (smart_indexer.py,
   non-git `detect_deletions=True` path)
5. `SmartIndexer._cleanup_multiple_visible_content_points` (reconcile)
6. `SmartIndexer.cleanup_branch_data` (branch cleanup)

The contract seam below makes an unopted read-only scroll raise, including
when these tests run against committed HEAD (where scroll's gate is itself
broken). Each test then checks real repair on an opted-in call or exposes
the caller's swallowed integrity failure.
"""

import hashlib
import json
from pathlib import Path

import pytest

from code_indexer.services.high_throughput_processor import HighThroughputProcessor
from code_indexer.services.smart_indexer import SmartIndexer
from code_indexer.storage.filesystem_vector_store import (
    FilesystemVectorStore,
    ScrollDataIntegrityError,
)
from tests.unit.services.test_smart_indexer_1969_round4_incremental_reprocess import (
    _corrupt_id_index_bin as _corrupt_id_index_bin_e2e,
    _duplicate_indexed_record,
    _init_repo,
    _make_smart_indexer,
    _run_git,
)

VECTOR_DIM = 4


def _write_repairable_duplicate_record(
    collection_dir: Path,
    *,
    project_id: str,
    file_hash: str,
    index: int,
    vector: list,
    shard_suffix: str,
    path: str = "src/foo.py",
) -> Path:
    unique_key = f"{project_id}_{file_hash}_{index}"
    point_id = hashlib.md5(unique_key.encode()).hexdigest()
    payload = {
        "path": path,
        "content": f"chunk content {index}{shard_suffix}",
        "language": "python",
        "project_id": project_id,
        "file_hash": file_hash,
        "chunk_index": index,
        "total_chunks": 1,
        "line_start": 1,
        "line_end": 10,
        "point_id": point_id,
        "unique_key": unique_key,
    }
    record = {"id": point_id, "vector": vector, "payload": payload}
    shard_dir = collection_dir / point_id[:2] / (point_id[2:4] + shard_suffix)
    shard_dir.mkdir(parents=True, exist_ok=True)
    file_path = shard_dir / f"vector_{point_id}.json"
    file_path.write_text(json.dumps(record))
    return file_path


def _add_hnsw_build_metadata(collection_dir: Path) -> None:
    meta_path = collection_dir / "collection_meta.json"
    meta = json.loads(meta_path.read_text())
    meta["hnsw_index"] = {
        "version": 1,
        "vector_dim": VECTOR_DIM,
        "space": "cosine",
        "vector_count": 0,
        "id_mapping": {},
    }
    meta_path.write_text(json.dumps(meta))


def _corrupt_id_index_bin(collection_dir: Path) -> None:
    (collection_dir / "id_index.bin").write_bytes(b"\xff\xff\xff\xff")


def _build_corrupt_collection_with_repairable_duplicate(tmp_path: Path, path: str):
    store = FilesystemVectorStore(base_path=tmp_path)
    store.create_collection("coll", vector_size=VECTOR_DIM)
    collection_path = tmp_path / "coll"
    _add_hnsw_build_metadata(collection_path)
    _write_repairable_duplicate_record(
        collection_path,
        project_id="proj",
        file_hash="sha256:round6caller",
        index=0,
        vector=[0.1, 0.2, 0.3, 0.4],
        shard_suffix="-a",
        path=path,
    )
    _write_repairable_duplicate_record(
        collection_path,
        project_id="proj",
        file_hash="sha256:round6caller",
        index=0,
        vector=[0.9, 0.9, 0.9, 0.9],
        shard_suffix="-b",
        path=path,
    )
    _corrupt_id_index_bin(collection_path)
    return store, collection_path


class _VectorStoreClientStandIn:
    """Minimal stand-in exposing only the attribute
    (`vector_store_client`) the method under test actually reads --
    avoids constructing a full `HighThroughputProcessor`/`SmartIndexer`
    (heavy git/config/embedding-provider wiring irrelevant to this
    method) while still calling the REAL, unbound production method
    against a REAL `FilesystemVectorStore`."""

    def __init__(self, vector_store_client):
        self.vector_store_client = vector_store_client


def _require_explicit_self_heal(
    monkeypatch: pytest.MonkeyPatch, store: FilesystemVectorStore
) -> None:
    """Model the intended read-only scroll contract in the HEAD export.

    Committed HEAD repairs even when self_heal is False. Without this seam,
    an indexing caller that omits the opt-in would appear to work on HEAD,
    and the test could not distinguish the caller bug from that older gate
    bug. Opted-in calls still run the real store method and real repair.
    """
    real_scroll = store.scroll_points

    def guarded_scroll(*args, **kwargs):
        if not kwargs.get("self_heal", False):
            raise ScrollDataIntegrityError("read-only scroll cannot repair")
        return real_scroll(*args, **kwargs)

    monkeypatch.setattr(store, "scroll_points", guarded_scroll)


class TestDeleteByFilterCallerAudit:
    def test_delete_by_filter_self_heals_instead_of_masking_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store, collection_path = _build_corrupt_collection_with_repairable_duplicate(
            tmp_path, "src/round6_delete.py"
        )
        _require_explicit_self_heal(monkeypatch, store)

        # "file_hash" is not a path-equality clause, so this bypasses the
        # PathIndex fast path and hits the same rglob-based duplicate
        # detection as scroll_points()'s no-filter callers.
        result = store.delete_by_filter(
            "coll",
            {"must": [{"key": "file_hash", "match": {"value": "sha256:round6caller"}}]},
        )

        assert result is True, (
            "delete_by_filter must self-heal the corrupt duplicate and "
            "complete successfully, not silently return False (its broad "
            "except-Exception handler currently masks the underlying "
            "ScrollDataIntegrityError as an ordinary failure)."
        )
        remaining = list(collection_path.rglob("vector_*.json"))
        assert remaining == [], (
            "delete_by_filter's scroll_points call must self-heal (wipe "
            "the implicated file's records) rather than leave the "
            "corrupt duplicate untouched."
        )


class TestFetchAllContentPointsCallerAudit:
    def test_fetch_all_content_points_self_heals(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store, collection_path = _build_corrupt_collection_with_repairable_duplicate(
            tmp_path, "src/round6_fetch_all.py"
        )
        _require_explicit_self_heal(monkeypatch, store)
        stand_in = _VectorStoreClientStandIn(store)

        points = HighThroughputProcessor._fetch_all_content_points(stand_in, "coll")  # type: ignore[arg-type]  # deliberate duck-typed stand-in, see class docstring

        assert isinstance(points, list)
        remaining = list(collection_path.rglob("vector_*.json"))
        assert remaining == [], (
            "_fetch_all_content_points (used to pre-fetch points shared "
            "between branch-visibility and branch-isolation-hiding "
            "mutation call sites) must self-heal a corrupt duplicate "
            "rather than hard-fail with ScrollDataIntegrityError."
        )


class TestScrollAllContentPointsCallerAudit:
    def test_scroll_all_content_points_self_heals(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store, collection_path = _build_corrupt_collection_with_repairable_duplicate(
            tmp_path, "src/round6_scroll_all.py"
        )
        _require_explicit_self_heal(monkeypatch, store)
        stand_in = _VectorStoreClientStandIn(store)

        points = SmartIndexer._scroll_all_content_points(stand_in, "coll")  # type: ignore[arg-type]  # deliberate duck-typed stand-in, see class docstring

        assert isinstance(points, list)
        remaining = list(collection_path.rglob("vector_*.json"))
        assert remaining == [], (
            "_scroll_all_content_points (used by "
            "_get_indexed_files_snapshot -> _do_reconcile_with_database) "
            "must self-heal a corrupt duplicate rather than hard-fail "
            "with ScrollDataIntegrityError, or `cidx index --reconcile` "
            "hard-fails on the exact corruption this whole fix exists "
            "to resolve."
        )


class TestReconcileVisibilityCleanupCallerAudit:
    def test_cleanup_scan_self_heals_before_visibility_updates(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        store, collection_path = _build_corrupt_collection_with_repairable_duplicate(
            tmp_path, "src/round6_visibility_cleanup.py"
        )
        _require_explicit_self_heal(monkeypatch, store)
        stand_in = _VectorStoreClientStandIn(store)

        SmartIndexer._cleanup_multiple_visible_content_points(stand_in, "coll", "main")  # type: ignore[arg-type]  # deliberate duck-typed stand-in, see class docstring

        assert list(collection_path.rglob("vector_*.json")) == [], (
            "The reconcile visibility-cleanup scan must opt in to repair; "
            "otherwise its broad exception handler hides the corrupt index."
        )


class TestDetectAndHandleDeletionsCallerAudit:
    def test_detect_and_handle_deletions_self_heals_instead_of_masking_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)

        victim_content = "# victim detect-deletions caller-audit content\n"
        (repo / "victim.py").write_text(victim_content)
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
        _corrupt_id_index_bin_e2e(collection_path)

        indexer2 = _make_smart_indexer(repo, metadata_path)
        _require_explicit_self_heal(monkeypatch, indexer2.vector_store_client)
        # Call the deletion-detection scan DIRECTLY -- bypassing
        # smart_index()'s own incremental/upsert pass entirely (which
        # would otherwise self-heal id_index.bin via a DIFFERENT,
        # already-self_heal=True write-path call before this method's
        # own scroll_points call ever runs) -- so THIS is genuinely the
        # first vector-store touch and must face the corruption itself.
        indexer2._detect_and_handle_deletions(None)

        remaining = list(collection_path.rglob("vector_*.json"))
        assert remaining == [], (
            "_detect_and_handle_deletions' own scroll_points call must "
            "self-heal the corrupt duplicate rather than silently mask "
            "the failure via its broad except-Exception handler (which "
            "currently just logs 'Database query failed during deletion "
            "detection' and returns, leaving the corruption in place "
            "forever)."
        )


class TestCleanupBranchDataCallerAudit:
    def test_branch_cleanup_opts_in_before_hiding_points(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A branch-cleanup write must not turn a duplicate into a silent no-op."""
        repo = tmp_path / "repo"
        repo.mkdir()
        _init_repo(repo)
        (repo / "victim.py").write_text("# indexed victim\n")
        _run_git(repo, "add", ".")
        _run_git(repo, "commit", "-m", "add example file")
        branch = _run_git(repo, "branch", "--show-current").stdout.strip()

        metadata_path = tmp_path / "metadata.json"
        first_indexer = _make_smart_indexer(repo, metadata_path)
        assert first_indexer.smart_index().files_processed >= 1
        collection_name = first_indexer.vector_store_client.resolve_collection_name(
            first_indexer.config, first_indexer.embedding_provider
        )
        collection_path = repo / ".code-indexer" / "index" / collection_name
        _duplicate_indexed_record(collection_path, "victim.py")
        victim_files = [
            path
            for path in collection_path.rglob("vector_*.json")
            if json.loads(path.read_text()).get("payload", {}).get("path")
            == "victim.py"
        ]
        assert len(victim_files) >= 2, "fixture did not create a real duplicate"
        _corrupt_id_index_bin_e2e(collection_path)

        indexer = _make_smart_indexer(repo, metadata_path)
        _require_explicit_self_heal(monkeypatch, indexer.vector_store_client)
        indexer.cleanup_branch_data(branch)

        pending_paths = (
            indexer.vector_store_client.get_pending_self_heal_reprocess_paths(
                collection_name
            )
        )
        assert "victim.py" in pending_paths, (
            "cleanup_branch_data silently returned zero counts without repairing "
            "the corrupt duplicate or recording its wiped file for replay"
        )
        assert not any(path.exists() for path in victim_files), (
            "cleanup_branch_data left the duplicate records on disk; its "
            "read-only scroll must explicitly opt in before branch cleanup"
        )
