"""RED/GREEN tests for Bug #1579: scroll_points self-heal on a pre-existing
shifted duplicate.

Bug #1579's write-path fix (test_filesystem_vector_store_1579_shifted_duplicate.py)
prevents NEW shifted duplicates from being created, but a collection built
before that fix (or reached via any other route) may already carry a
duplicate point_id across two vector_*.json files in different shard
directories. scroll_points' SHARDED_JSON enumeration
(_build_sharded_json_scroll_index) raises ScrollDataIntegrityError the
moment it finds a duplicate, which propagates through smart_indexer.py's
fail-fast reconcile and permanently kills `cidx index --reconcile` for the
affected repo.

These tests build a REAL FilesystemVectorStore collection via real
begin_indexing/upsert_points/end_indexing (so collection_meta.json's
hnsw_index metadata is production-shaped), then directly inject raw
vector_*.json files simulating a pre-existing on-disk shifted duplicate
(bypassing the now-fixed upsert_points write path -- exactly what a
legacy/pre-fix collection looks like). No mocking of the store under test;
one test wraps the REAL repair_duplicate_and_shifted_points with a
call-counting spy (via monkeypatch) that still delegates to the real
implementation, to prove the self-heal retries at most once without
altering its behavior.
"""

import json

import pytest

from code_indexer.storage.filesystem_vector_store import (
    FilesystemVectorStore,
    ScrollDataIntegrityError,
)
from code_indexer.storage.id_index_manager import IDIndexManager
from code_indexer.storage.shared.collection_dedup_repair import (
    DedupRepairAmbiguousError,
)
import code_indexer.storage.shared.collection_dedup_repair as collection_dedup_repair_mod
from tests.unit.storage.shared.test_collection_dedup_repair_1502 import (
    _point_id,
    _write_record,
)


def _register_winner_in_id_index(collection_path, point_id, winner_path) -> None:
    """Mirror the real production state Bug #1579 produces: id_index.bin
    tracks the LATEST write location for a re-upserted point_id (set via
    upsert_points' in-memory _id_index, persisted at end_indexing) -- only
    the OLD file is orphaned on disk, the id is never simply ABSENT from
    the index. Without this, a fabricated duplicate has no resolvable
    winner and repair deletes the point entirely (whole-group-deleted),
    which is a different, unrealistic case from the one this bug reports.
    """
    manager = IDIndexManager()
    id_index = manager.load_index(collection_path)
    id_index[point_id] = winner_path
    manager.save_index(collection_path, id_index)


VECTOR_DIM = 4
PROJECT_ID = "proj"
# The normal points and the injected duplicate pair use DIFFERENT file_hash
# values so they fall into DIFFERENT _plan_renumber groups (grouped by
# (project_id, file_hash)) -- mirroring realistic distinct source files.
# Mixing them into one group would trip _plan_renumber's unrelated "mix of
# records with and without line_start" hard-fail whenever one side carries
# line_start/line_end and the other doesn't.
FILE_HASH = "sha256:aaaa"
DUP_FILE_HASH = "sha256:bbbb"


def _unique_key(index: int, file_hash: str = FILE_HASH) -> str:
    return f"{PROJECT_ID}_{file_hash}_{index}"


@pytest.fixture
def store_with_normal_points(tmp_path):
    """A real, correctly-shaped collection with two normal points (built via
    the real upsert_points/end_indexing lifecycle) whose ids/unique_keys are
    self-consistent (id == md5(unique_key)) so the whole-collection identity
    gate in repair_duplicate_and_shifted_points can pass. Both carry
    line_start/line_end so their shared renumber group is internally
    consistent (no mix with/without line info).
    """
    store = FilesystemVectorStore(base_path=tmp_path, project_root=tmp_path)
    store.create_collection("coll", vector_size=VECTOR_DIM)
    collection_path = tmp_path / "coll"

    store.begin_indexing("coll")
    store.upsert_points(
        "coll",
        [
            {
                "id": _point_id(PROJECT_ID, FILE_HASH, 0),
                "vector": [0.1, 0.2, 0.3, 0.4],
                "payload": {
                    "path": "src/a.py",
                    "unique_key": _unique_key(0),
                    "line_start": 1,
                    "line_end": 2,
                },
            },
            {
                "id": _point_id(PROJECT_ID, FILE_HASH, 1),
                "vector": [0.5, 0.6, 0.7, 0.8],
                "payload": {
                    "path": "src/a.py",
                    "unique_key": _unique_key(1),
                    "line_start": 3,
                    "line_end": 4,
                },
            },
        ],
    )
    store.end_indexing("coll")
    return store, collection_path


def _inject_duplicate_pair(collection_path):
    """Write two vector_*.json files sharing the SAME point_id into
    different shard directories -- bypassing upsert_points entirely, exactly
    reproducing what a pre-#1579-fix collection looks like on disk. Returns
    the shared duplicate point_id.

    Uses chunk index 0 (the sole chunk in its own isolated DUP_FILE_HASH
    group) so it is already canonical -- _plan_renumber's renumbering step
    (a real, separate repair behavior, not part of dedup itself) assigns
    the SAME point_id back rather than reassigning a new one, keeping the
    duplicate's identity stable across the repair for this test's purposes.
    """
    dup_id = _point_id(PROJECT_ID, DUP_FILE_HASH, 0)
    winner_path = _write_record(
        collection_path,
        project_id=PROJECT_ID,
        file_hash=DUP_FILE_HASH,
        index=0,
        vector=[0.9, 0.8, 0.7, 0.6],
        line_start=1,
        line_end=2,
        shard_suffix="",
    )
    _write_record(
        collection_path,
        project_id=PROJECT_ID,
        file_hash=DUP_FILE_HASH,
        index=0,
        vector=[0.05, 0.04, 0.03, 0.02],
        line_start=1,
        line_end=2,
        shard_suffix="_dup",
    )
    _register_winner_in_id_index(collection_path, dup_id, winner_path)
    return dup_id


class TestScrollSelfHealOnPreExistingDuplicate:
    def test_scroll_self_heals_pre_existing_duplicate(self, store_with_normal_points):
        """Bug #1579: scroll_points must self-heal a pre-existing on-disk
        shifted duplicate instead of permanently raising
        ScrollDataIntegrityError. Pre-fix this raises; post-fix it resolves
        the duplicate down to one surviving record and returns a valid page.
        """
        store, collection_path = store_with_normal_points
        dup_id = _inject_duplicate_pair(collection_path)

        dup_files_before = [
            f for f in collection_path.rglob("vector_*.json") if dup_id in f.name
        ]
        assert len(dup_files_before) == 2, (
            "sanity: fixture actually created a duplicate pair"
        )

        points, next_offset = store.scroll_points("coll", limit=100)

        returned_ids = [p["id"] for p in points]
        assert dup_id in returned_ids, "the repaired point must still be returned"
        assert len(returned_ids) == len(set(returned_ids)), (
            "no duplicate rows in the result page"
        )

        surviving_dup_files = [
            f for f in collection_path.rglob("vector_*.json") if dup_id in f.name
        ]
        assert len(surviving_dup_files) == 1, (
            "repair must resolve the duplicate down to exactly one file, found "
            f"{len(surviving_dup_files)}"
        )

    def test_scroll_attempts_repair_at_most_once_then_reraises(
        self, store_with_normal_points, monkeypatch
    ):
        """Bounded-retry proof: when repair CANNOT resolve the duplicate
        (here, because an unrelated record elsewhere in the collection fails
        the whole-collection identity gate, so repair performs zero
        mutation), scroll_points must attempt repair EXACTLY ONCE and then
        re-raise the original ScrollDataIntegrityError -- never loop.
        The real repair function is wrapped with a call-counting spy that
        still delegates to the real implementation (no behavior is mocked).
        """
        store, collection_path = store_with_normal_points

        # A record with NO unique_key breaks the whole-collection identity
        # gate, so repair_duplicate_and_shifted_points performs zero
        # mutation (gate_passed=False) -- the duplicate below is left
        # unresolved, forcing scroll's SECOND attempt to hit the identical
        # ScrollDataIntegrityError.
        _write_record(
            collection_path,
            project_id=PROJECT_ID,
            file_hash=FILE_HASH,
            index=3,
            vector=[0.2, 0.2, 0.2, 0.2],
            line_start=1,
            line_end=2,
            omit_unique_key=True,
        )
        _inject_duplicate_pair(collection_path)

        real_repair = collection_dedup_repair_mod.repair_duplicate_and_shifted_points
        call_count = {"n": 0}

        def counting_repair(*args, **kwargs):
            call_count["n"] += 1
            return real_repair(*args, **kwargs)

        monkeypatch.setattr(
            collection_dedup_repair_mod,
            "repair_duplicate_and_shifted_points",
            counting_repair,
        )

        with pytest.raises(ScrollDataIntegrityError):
            store.scroll_points("coll", limit=100)

        assert call_count["n"] == 1, (
            "repair must be attempted at most once per scroll_points call, "
            f"was called {call_count['n']} times"
        )

    def test_scroll_propagates_ambiguous_error_for_malformed_record(
        self, store_with_normal_points
    ):
        """A malformed record (non-string 'id') alongside a genuine
        duplicate pair makes repair refuse to touch ANYTHING
        (DedupRepairAmbiguousError) -- scroll_points must surface this
        DISTINCT, more actionable error rather than the original
        ScrollDataIntegrityError or a silently-swallowed generic failure,
        proving self-healing genuinely invoked repair rather than merely
        re-surfacing its own parse error.
        """
        store, collection_path = store_with_normal_points
        _inject_duplicate_pair(collection_path)

        malformed_dir = collection_path / "zz" / "yy"
        malformed_dir.mkdir(parents=True, exist_ok=True)
        (malformed_dir / "vector_bad.json").write_text(
            json.dumps(
                {
                    # non-string id -- malformed per both scroll's own
                    # parser and repair's _scan_raw_records classifier.
                    "id": 12345,
                    "vector": [0.1, 0.2, 0.3, 0.4],
                    "payload": {"path": "src/c.py"},
                }
            )
        )

        with pytest.raises(DedupRepairAmbiguousError):
            store.scroll_points("coll", limit=100)
