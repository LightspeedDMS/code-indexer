"""Integration tests for Bug #1969 code review finding F1 (P2 BLOCKING):
FilesystemVectorStore._load_id_index()'s corrupt-index-rebuild branch is
the exact function the review flagged as reachable from BOTH write paths
(cidx index / upsert / end-of-indexing) AND query-time read paths (search,
count_points, list_files, the daemon cache). The self-heal added for Bug
#1969 must only fire when a caller explicitly requests it via
self_heal=True -- every other caller must keep the original
hard-fail-immediately behavior (zero mutation).

Uses a REAL FilesystemVectorStore (create_collection() for authentic
scaffolding) with a hand-corrupted id_index.bin and a hand-constructed
repairable duplicate point_id pair -- no mocking of the code under test.

RESOLVED architectural nuance (Bug #1969 Round 3): earlier revisions of
this docstring documented an unresolved limitation discovered while
writing these tests -- _load_id_index()'s CorruptIDIndexError branch is
the ONLY way FilesystemVectorStore ever reaches rebuild_from_vectors()
for a non-temporal SHARDED_JSON collection (a successfully-LOADED
id_index.bin short-circuits at `if index: return index`, never scanning
the raw vector_*.json files at all), and repair_duplicate_and_shifted_
points()'s own winner-resolution (_plan_dedup) ALSO reads id_index.bin as
its sole source of "which copy is the trustworthy winner" -- AC30 (Story
#1560) makes it refuse (DedupRepairAmbiguousError(reason=
DedupRepairAmbiguousReason.CORRUPT_ID_INDEX)) rather than guess when
id_index.bin itself fails to load. Since both reads targeted the SAME
physically-corrupt file, self-heal reached via exactly THIS trigger could
not fully resolve a duplicate through the normal per-chunk-winner repair.

Round 3 closes this gap: IDIndexManager.rebuild_from_vectors() now
escalates that ONE specific reason (and only that one) to
collection_dedup_repair.recover_from_corrupt_id_index_by_wiping_files(),
which does not depend on id_index.bin at all -- it deletes every
vector_*.json record belonging to every file implicated by a duplicate
point_id group (the whole file, not just the colliding chunks) and
rebuilds id_index.bin/HNSW from the remaining, conflict-free records.
See the second test below: self_heal=True now FULLY resolves this exact
scenario (both colliding records gone, the file left wholly missing so
it is fully reprocessed on the next real re-index -- see that function's
own docstring for why a wholly-missing file, not a half-indexed one, is
the safe outcome for this project's blob-hash/mtime-only reconcile).
Every OTHER DedupRepairAmbiguousError reason still hard-fails, unchanged.
"""

import json
from pathlib import Path

import pytest

from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
from code_indexer.storage.id_index_manager import DuplicateSourceIdError, IDIndexManager

VECTOR_DIM = 4


def _write_repairable_duplicate_record(
    collection_dir: Path,
    *,
    project_id: str,
    file_hash: str,
    index: int,
    vector: list,
    shard_suffix: str,
) -> Path:
    """Same real production identity scheme as
    test_id_index_manager_1969_rebuild_self_heal.py's helper: unique_key =
    f"{project_id}_{file_hash}_{index}", point_id = md5(unique_key)."""
    import hashlib

    unique_key = f"{project_id}_{file_hash}_{index}"
    point_id = hashlib.md5(unique_key.encode()).hexdigest()
    payload = {
        "path": "src/foo.py",
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


def _corrupt_id_index_bin(collection_dir: Path) -> None:
    """Invalid header bytes -- triggers CorruptIDIndexError on load,
    exactly like tests/unit/storage/test_id_index_manager.py's own
    test_corrupted_file_handling."""
    (collection_dir / "id_index.bin").write_bytes(b"\xff\xff\xff\xff")


def _add_hnsw_build_metadata(collection_dir: Path) -> None:
    """repair_duplicate_and_shifted_points() requires collection_meta.json's
    hnsw_index.vector_dim/.space as the sole authoritative HNSW build
    params -- matching a collection that has genuinely been through one
    real HNSW build (a legitimate precondition for the repair to run)."""
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


def _build_corrupt_collection_with_repairable_duplicate(tmp_path: Path):
    """Real create_collection() scaffolding + a hand-corrupted id_index.bin
    + a hand-constructed repairable duplicate pair -- this exact shape
    (pre-existing corruption + a pre-Bug-#1502 duplicate) cannot arise
    through the normal upsert_points() write path (its own dedup-on-write
    logic prevents it within one run), so it must be constructed directly,
    matching Bug #1583's established test convention."""
    store = FilesystemVectorStore(base_path=tmp_path)
    store.create_collection("coll", vector_size=VECTOR_DIM)
    collection_path = tmp_path / "coll"
    _add_hnsw_build_metadata(collection_path)
    winner_path = _write_repairable_duplicate_record(
        collection_path,
        project_id="proj",
        file_hash="sha256:integration",
        index=0,
        vector=[0.1, 0.2, 0.3, 0.4],
        shard_suffix="-a",
    )
    loser_path = _write_repairable_duplicate_record(
        collection_path,
        project_id="proj",
        file_hash="sha256:integration",
        index=0,
        vector=[0.9, 0.9, 0.9, 0.9],
        shard_suffix="-b",
    )
    _corrupt_id_index_bin(collection_path)
    return store, collection_path, winner_path, loser_path


class TestLoadIdIndexSelfHealGating:
    """FilesystemVectorStore._load_id_index() -- the exact function named
    in the F1 finding -- must gate Bug #1969's self-heal behind an
    explicit self_heal parameter."""

    def test_default_never_self_heals_a_repairable_duplicate(
        self, tmp_path: Path
    ) -> None:
        (
            store,
            collection_path,
            winner_path,
            loser_path,
        ) = _build_corrupt_collection_with_repairable_duplicate(tmp_path)

        with pytest.raises(DuplicateSourceIdError):
            store._load_id_index("coll")  # self_heal omitted -> read-path default

        # Zero mutation: both records untouched.
        assert winner_path.exists()
        assert loser_path.exists()

    def test_explicit_self_heal_true_now_fully_resolves_via_round3_whole_file_wipe(
        self, tmp_path: Path
    ) -> None:
        """See module docstring (Bug #1969 Round 3): AC30 (Story #1560)
        still refuses to pick a PER-CHUNK winner when id_index.bin itself
        fails to load (both the initial scan AND the repair's own
        winner lookup would hit the SAME corrupt file) -- but
        rebuild_from_vectors() now escalates that specific case to a
        whole-file-wipe recovery instead of propagating
        DedupRepairAmbiguousError. self_heal=True therefore now FULLY
        resolves this exact scenario: both colliding records (the whole
        implicated file, not just one loser) are deleted, and the call
        completes successfully."""
        (
            store,
            collection_path,
            winner_path,
            loser_path,
        ) = _build_corrupt_collection_with_repairable_duplicate(tmp_path)

        result = store._load_id_index("coll", self_heal=True)

        assert isinstance(result, dict)
        # Whole-file wipe: BOTH colliding records are gone -- there is no
        # "winner" to preserve when id_index.bin cannot say who it was.
        assert not winner_path.exists()
        assert not loser_path.exists()
        assert len(result) == 0


class TestRebuildFromVectorsFullyResolvesWithAValidIdIndex:
    """Full end-to-end resolution IS achievable against a real
    FilesystemVectorStore-scaffolded collection when self_heal=True is
    requested and id_index.bin is present, VALID, and already names a
    winner -- the shape a direct, unconditional caller of
    rebuild_from_vectors() (not gated behind a corrupt-index check) would
    encounter."""

    def test_direct_self_heal_call_resolves_duplicate_against_real_scaffolding(
        self, tmp_path: Path
    ) -> None:
        store = FilesystemVectorStore(base_path=tmp_path)
        store.create_collection("coll", vector_size=VECTOR_DIM)
        collection_path = tmp_path / "coll"
        _add_hnsw_build_metadata(collection_path)
        winner_path = _write_repairable_duplicate_record(
            collection_path,
            project_id="proj",
            file_hash="sha256:validindex",
            index=0,
            vector=[0.1, 0.2, 0.3, 0.4],
            shard_suffix="-a",
        )
        loser_path = _write_repairable_duplicate_record(
            collection_path,
            project_id="proj",
            file_hash="sha256:validindex",
            index=0,
            vector=[0.9, 0.9, 0.9, 0.9],
            shard_suffix="-b",
        )
        import hashlib

        shared_point_id = hashlib.md5("proj_sha256:validindex_0".encode()).hexdigest()
        # A VALID, loadable id_index.bin naming the winner -- NOT corrupt.
        IDIndexManager().save_index(collection_path, {shared_point_id: winner_path})

        result = IDIndexManager().rebuild_from_vectors(collection_path, self_heal=True)

        assert len(result) == 1
        assert not loser_path.exists()


class TestCallSiteSelfHealFlags:
    """The reviewer's fix direction names the exact write-path callers that
    must opt in: upsert_points()/end_indexing() (indexing) and
    temporal_reconciliation.py's _reconcile_shard_legacy (covered
    separately). A genuine read path (count_points()) must never request
    self_heal=True. Verified via a spy around _load_id_index() capturing
    the kwarg each real call site actually passes -- no corruption/
    duplicate needed here, since the kwarg is passed unconditionally
    regardless of whether the except branch ever fires."""

    def _spy_on_load_id_index(self, monkeypatch):
        captured: list = []
        original = FilesystemVectorStore._load_id_index

        def _spy(self, collection_name, subdirectory=None, **kwargs):
            captured.append(kwargs.get("self_heal", "OMITTED"))
            return original(self, collection_name, subdirectory, **kwargs)

        monkeypatch.setattr(FilesystemVectorStore, "_load_id_index", _spy)
        return captured

    def test_upsert_points_requests_self_heal_true(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # create_collection() pre-populates the in-memory _id_index cache
        # entry to {} -- a FRESH reader instance (no in-memory cache at
        # all) attached to the SAME on-disk collection is what forces
        # upsert_points()'s "ensure ID index exists" check to actually
        # call _load_id_index().
        writer = FilesystemVectorStore(base_path=tmp_path)
        writer.create_collection("coll", vector_size=VECTOR_DIM)
        store = FilesystemVectorStore(base_path=tmp_path)
        captured = self._spy_on_load_id_index(monkeypatch)

        store.upsert_points(
            "coll",
            [{"id": "p1", "vector": [0.1, 0.2, 0.3, 0.4], "payload": {"path": "a.py"}}],
        )

        assert captured, "_load_id_index must have been called by upsert_points"
        assert all(v is True for v in captured), (
            f"upsert_points must request self_heal=True on every "
            f"_load_id_index call it makes; got {captured}"
        )

    def test_end_indexing_requests_self_heal_true_on_empty_cache(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        # Fresh reader instance, no prior upsert in this process -- exercises
        # end_indexing()'s own "reconciliation found nothing new" cache-miss
        # branch, which calls _load_id_index() directly.
        writer = FilesystemVectorStore(base_path=tmp_path)
        writer.create_collection("coll", vector_size=VECTOR_DIM)
        store = FilesystemVectorStore(base_path=tmp_path)
        captured = self._spy_on_load_id_index(monkeypatch)

        store.end_indexing("coll")

        assert captured, "_load_id_index must have been called by end_indexing"
        assert all(v is True for v in captured), (
            f"end_indexing must request self_heal=True on every "
            f"_load_id_index call it makes; got {captured}"
        )

    def test_count_points_never_requests_self_heal_true(
        self, tmp_path: Path, monkeypatch
    ) -> None:
        writer = FilesystemVectorStore(base_path=tmp_path)
        writer.create_collection("coll", vector_size=VECTOR_DIM)
        # Force the fallback (non-metadata-fast-path) branch of
        # count_points() so it actually reaches _load_id_index().
        meta_path = tmp_path / "coll" / "collection_meta.json"
        meta = json.loads(meta_path.read_text())
        meta.pop("hnsw_index", None)
        meta_path.write_text(json.dumps(meta))
        store = FilesystemVectorStore(base_path=tmp_path)
        captured = self._spy_on_load_id_index(monkeypatch)

        store.count_points("coll")

        assert captured, "_load_id_index must have been called by count_points"
        assert all(v is not True for v in captured), (
            f"count_points is a read/query path -- it must NEVER request "
            f"self_heal=True; got {captured}"
        )
