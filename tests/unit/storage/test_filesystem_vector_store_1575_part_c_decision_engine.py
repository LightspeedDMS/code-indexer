"""TDD tests for Bug #1575 Part C -- the full visibility-epoch decision
engine (end_indexing()'s reuse / incremental / full-rebuild algorithm),
proven via REAL query-result correctness, not code-path inspection.

Real FilesystemVectorStore + real HNSW + real filesystem/SQLite throughout.
`precomputed_query_vector` bypasses the need for a real embedding provider
(a legitimate, first-class supported bypass -- not a mock of the code under
test); `_UnusedEmbeddingProvider` is a plain, never-invoked placeholder.

Membership (`in`/`not in`) assertions are used against query result sets
whenever more than one visible point coexists in the collection at that
moment -- `search()` can legitimately return ALL visible points ranked by
similarity (not just the nearest neighbour of the exact vector queried),
so an exact-set-equality assertion would be invalid there. Exact-set
equality is used only where the collection genuinely holds a single
queryable point at that moment.
"""

import json

import numpy as np

from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
from code_indexer.storage.shared.hnsw_sync_state import HNSW_SYNC_SCHEMA_VERSION

VECTOR_DIM = 16


class _UnusedEmbeddingProvider:
    """Placeholder passed as `embedding_provider` -- never invoked because
    every search() call below supplies `precomputed_query_vector`."""


def _vector(seed: int):
    rng = np.random.default_rng(seed)
    return rng.standard_normal(VECTOR_DIM).astype(np.float32)


def _point(point_id, path, seed, hidden_branches=None):
    return {
        "id": point_id,
        "vector": _vector(seed).tolist(),
        "payload": {
            "path": path,
            "type": "content",
            "hidden_branches": hidden_branches or [],
        },
    }


def _query_ids(store, collection_name, seed, limit=50):
    results = store.search(
        query="unused",
        embedding_provider=_UnusedEmbeddingProvider(),
        collection_name=collection_name,
        limit=limit,
        precomputed_query_vector=_vector(seed).tolist(),
    )
    return {r["id"] for r in results}


def _hnsw_sync(collection_path):
    """Bug #1619: resolve hnsw_sync the same way production code does --
    prefer the dedicated hnsw_sync_state.json file, falling back to the
    legacy embedded collection_meta.json key for pre-migration collections."""
    sync_file = collection_path / "hnsw_sync_state.json"
    if sync_file.exists():
        return json.loads(sync_file.read_text())
    meta_file = collection_path / "collection_meta.json"
    if not meta_file.exists():
        return None
    meta = json.loads(meta_file.read_text())
    return meta.get("hnsw_sync")


def _hnsw_index_identity(collection_path):
    st = (collection_path / "hnsw_index.bin").stat()
    return (st.st_ino, st.st_mtime_ns, st.st_size)


def _hnsw_index_bytes(collection_path):
    return (collection_path / "hnsw_index.bin").read_bytes()


# --- AC8: byte-for-byte reuse on an unchanged refresh ----------------------


def test_ac8_unchanged_refresh_reuses_index_and_hidden_files_stay_hidden(tmp_path):
    store = FilesystemVectorStore(base_path=tmp_path)
    store.create_collection("coll", vector_size=VECTOR_DIM)
    store.begin_indexing("coll")
    store.upsert_points(
        "coll",
        [_point("v", "src/visible.py", 1), _point("h", "src/hidden.py", 2)],
    )
    store.set_hnsw_branch_context(
        "coll",
        "main",
        {"src/visible.py"},  # hidden.py not on this branch
    )
    store.end_indexing("coll")

    assert _query_ids(store, "coll", 1) == {"v"}
    assert "h" not in _query_ids(store, "coll", 2)

    collection_path = tmp_path / "coll"
    identity_before = _hnsw_index_identity(collection_path)
    bytes_before = _hnsw_index_bytes(collection_path)

    # Second refresh: SAME branch context, ZERO mutations.
    store.begin_indexing("coll")
    store.set_hnsw_branch_context("coll", "main", {"src/visible.py"})
    store.end_indexing("coll")

    identity_after = _hnsw_index_identity(collection_path)
    bytes_after = _hnsw_index_bytes(collection_path)
    assert identity_after == identity_before, (
        "HNSW artifact must be reused byte-for-byte"
    )
    assert bytes_after == bytes_before, "HNSW file content must be byte-identical"
    assert _query_ids(store, "coll", 1) == {"v"}
    assert "h" not in _query_ids(store, "coll", 2)


# --- AC42: pre-existing collection with no hnsw_sync bootstraps ONCE ------


def test_ac42_pre_existing_collection_bootstraps_with_exactly_one_full_rebuild(
    tmp_path,
):
    store = FilesystemVectorStore(base_path=tmp_path)
    store.create_collection("coll", vector_size=VECTOR_DIM)
    store.begin_indexing("coll")
    store.upsert_points("coll", [_point("a", "src/a.py", 1)])
    store.end_indexing("coll")

    collection_path = tmp_path / "coll"
    assert _hnsw_sync(collection_path) is not None  # created by that first run

    # Simulate a collection indexed by pre-Part-C code: remove the hnsw_sync
    # state entirely, matching a genuinely pre-existing collection. Bug
    # #1619: hnsw_sync now lives in its own dedicated file (never embedded
    # in collection_meta.json by current code), so removing it means
    # deleting that dedicated file.
    (collection_path / "hnsw_sync_state.json").unlink()
    assert _hnsw_sync(collection_path) is None

    identity_before_bootstrap = _hnsw_index_identity(collection_path)

    # set_hnsw_branch_context is called on EVERY round below, matching how
    # production's git-aware refresh path always registers branch context
    # (hide_files_not_in_branch_thread_safe runs unconditionally) -- without
    # it, no HNSWSyncSession would ever exist for a zero-mutation round and
    # the decision engine's fail-safe "no session tracked" branch would
    # (correctly, per spec) force a full rebuild every time, matching
    # pre-Part-C behavior exactly for that untracked case.
    store2 = FilesystemVectorStore(base_path=tmp_path)
    store2.begin_indexing("coll")
    store2.set_hnsw_branch_context("coll", "main", {"src/a.py"})
    store2.end_indexing("coll")  # zero mutations this session -> bootstrap rebuild

    identity_after_bootstrap = _hnsw_index_identity(collection_path)
    assert identity_after_bootstrap != identity_before_bootstrap, (
        "the bootstrap run must have performed a real rebuild"
    )
    assert _hnsw_sync(collection_path) is not None
    assert _query_ids(store2, "coll", 1) == {"a"}

    # A SUBSEQUENT no-op run (SAME branch context) must now REUSE (proving
    # the bootstrap was exactly one rebuild, not a recurring one every
    # session).
    store2.begin_indexing("coll")
    store2.set_hnsw_branch_context("coll", "main", {"src/a.py"})
    store2.end_indexing("coll")
    identity_after_second_run = _hnsw_index_identity(collection_path)
    assert identity_after_second_run == identity_after_bootstrap


# --- AC10: malformed hnsw_sync forces a full rebuild -----------------------


def test_ac10_malformed_hnsw_sync_forces_full_rebuild_then_reuses(tmp_path):
    store = FilesystemVectorStore(base_path=tmp_path)
    store.create_collection("coll", vector_size=VECTOR_DIM)
    store.begin_indexing("coll")
    store.upsert_points("coll", [_point("a", "src/a.py", 1)])
    store.end_indexing("coll")

    collection_path = tmp_path / "coll"
    # Bug #1619: hnsw_sync now lives in its own dedicated file.
    sync_file = collection_path / "hnsw_sync_state.json"
    sync_state = json.loads(sync_file.read_text())
    sync_state["mutation_epoch"] = -5  # malformed: negative epoch
    sync_file.write_text(json.dumps(sync_state))

    identity_before = _hnsw_index_identity(collection_path)

    # set_hnsw_branch_context on every round -- see test_ac42's identical
    # rationale: without it no session exists for a zero-mutation round and
    # the fail-safe "no session tracked" branch forces a full rebuild.
    store2 = FilesystemVectorStore(base_path=tmp_path)
    store2.begin_indexing("coll")
    store2.set_hnsw_branch_context("coll", "main", {"src/a.py"})
    store2.end_indexing("coll")

    identity_after_repair = _hnsw_index_identity(collection_path)
    assert identity_after_repair != identity_before, (
        "malformed state must trigger a real full rebuild, not a silent skip"
    )
    sync_after = _hnsw_sync(collection_path)
    assert sync_after["mutation_epoch"] >= 0
    assert sync_after["status"] == "clean"
    assert _query_ids(store2, "coll", 1) == {"a"}

    # A subsequent no-op run (SAME branch context) must now REUSE the
    # repaired artifact.
    store2.begin_indexing("coll")
    store2.set_hnsw_branch_context("coll", "main", {"src/a.py"})
    store2.end_indexing("coll")
    assert _hnsw_index_identity(collection_path) == identity_after_repair


# --- AC48: branch switch between identical visible-path sets --------------


def test_ac48_branch_switch_identical_visible_set_still_forces_full_rebuild(tmp_path):
    store = FilesystemVectorStore(base_path=tmp_path)
    store.create_collection("coll", vector_size=VECTOR_DIM)
    store.begin_indexing("coll")
    store.upsert_points("coll", [_point("a", "src/a.py", 1)])
    store.set_hnsw_branch_context("coll", "main", {"src/a.py"})
    store.end_indexing("coll")

    collection_path = tmp_path / "coll"
    identity_before = _hnsw_index_identity(collection_path)

    # A FRESH process/session observing the SAME durable state, with the
    # SAME visible_files set but a DIFFERENT branch.
    store2 = FilesystemVectorStore(base_path=tmp_path)
    store2.begin_indexing("coll")
    store2.set_hnsw_branch_context("coll", "feature-x", {"src/a.py"})
    store2.end_indexing("coll")

    identity_after = _hnsw_index_identity(collection_path)
    # A branch switch must ALWAYS force a full rebuild -- proven here by the
    # artifact's identity changing even though nothing else did (an
    # unchanged inode/mtime would indicate an incorrect reuse-by-path-set).
    assert identity_after != identity_before
    sync_after = _hnsw_sync(collection_path)
    assert sync_after["current_branch"] == "feature-x"


# --- AC11(b): hidden_branches change alone (unchanged visible_files set) --


def test_ac11b_hidden_branches_change_alone_removes_point_from_query(tmp_path):
    store = FilesystemVectorStore(base_path=tmp_path)
    store.create_collection("coll", vector_size=VECTOR_DIM)
    store.begin_indexing("coll")
    store.upsert_points(
        "coll", [_point("a", "src/a.py", 1), _point("b", "src/b.py", 2)]
    )
    store.set_hnsw_branch_context("coll", "main", {"src/a.py", "src/b.py"})
    store.end_indexing("coll")

    assert "b" in _query_ids(store, "coll", 2)

    # Hide "b" via hidden_branches alone -- visible_files set is UNCHANGED.
    store.begin_indexing("coll")
    store.set_hnsw_branch_context("coll", "main", {"src/a.py", "src/b.py"})
    store._batch_update_payload_only(
        [{"id": "b", "payload": {"hidden_branches": ["main"]}}], "coll"
    )
    store.end_indexing("coll")

    assert "b" not in _query_ids(store, "coll", 2)
    # Only "a" remains queryable now that "b" is hidden.
    assert _query_ids(store, "coll", 1) == {"a"}


# --- AC11(c)/(d): add / replace / remove for an already-visible file -------


def test_ac11cd_add_replace_remove_for_already_visible_file(tmp_path):
    store = FilesystemVectorStore(base_path=tmp_path)
    store.create_collection("coll", vector_size=VECTOR_DIM)
    store.begin_indexing("coll")
    store.upsert_points("coll", [_point("a1", "src/a.py", 1)])
    store.set_hnsw_branch_context("coll", "main", {"src/a.py"})
    store.end_indexing("coll")
    assert _query_ids(store, "coll", 1) == {"a1"}

    # (c) ADD a second chunk for the same already-visible file. Both a1 and
    # a2 are upserted TOGETHER (a realistic per-file chunking pass produces
    # every chunk for one file in one batch) -- upserting a2 ALONE for a
    # path that already held a1 would trigger Story #540's existing
    # orphan-cleanup (a re-index of a file replaces ALL its previously
    # stored chunks with the new batch), incorrectly evicting a1.
    store.begin_indexing("coll")
    store.upsert_points(
        "coll", [_point("a1", "src/a.py", 1), _point("a2", "src/a.py", 3)]
    )
    store.set_hnsw_branch_context("coll", "main", {"src/a.py"})
    store.end_indexing("coll")
    assert "a2" in _query_ids(store, "coll", 3)
    assert "a1" in _query_ids(store, "coll", 1)

    # (d) REPLACE a1's vector content (same id, new vector). a2 is
    # re-supplied UNCHANGED in the same call for the same Story #540
    # orphan-cleanup reason as the "ADD" step above -- upserting a1 alone
    # would prematurely orphan a2 before the deliberate "remove a2" step
    # below.
    store.begin_indexing("coll")
    store.upsert_points(
        "coll", [_point("a1", "src/a.py", 4), _point("a2", "src/a.py", 3)]
    )
    store.set_hnsw_branch_context("coll", "main", {"src/a.py"})
    store.end_indexing("coll")
    assert "a1" in _query_ids(store, "coll", 4)
    assert "a2" in _query_ids(store, "coll", 3)

    # (d) REMOVE a2 entirely.
    store.begin_indexing("coll")
    store.delete_points("coll", ["a2"])
    store.set_hnsw_branch_context("coll", "main", {"src/a.py"})
    store.end_indexing("coll")
    assert "a2" not in _query_ids(store, "coll", 3)
    # Only "a1" remains queryable now that "a2" is deleted.
    assert _query_ids(store, "coll", 4) == {"a1"}


# --- AC11(f): corrupt hnsw_index.bin file forces full rebuild -------------
# (Narrow scope, deliberately: hnsw_sync METADATA corruption is AC10's
# separate, already-covered concern above -- this test corrupts the HNSW
# binary artifact itself.)


def test_ac11f_corrupt_hnsw_index_bin_file_forces_full_rebuild(tmp_path):
    store = FilesystemVectorStore(base_path=tmp_path)
    store.create_collection("coll", vector_size=VECTOR_DIM)
    store.begin_indexing("coll")
    store.upsert_points("coll", [_point("a", "src/a.py", 1)])
    store.set_hnsw_branch_context("coll", "main", {"src/a.py"})
    store.end_indexing("coll")

    collection_path = tmp_path / "coll"
    (collection_path / "hnsw_index.bin").write_bytes(b"not a real hnsw index")

    store.begin_indexing("coll")
    store.upsert_points("coll", [_point("b", "src/b.py", 2)])
    store.set_hnsw_branch_context("coll", "main", {"src/a.py", "src/b.py"})
    store.end_indexing("coll")

    ids = _query_ids(store, "coll", 1) | _query_ids(store, "coll", 2)
    assert ids == {"a", "b"}


# --- AC49: epoch overflow forces a full rebuild with an exact reset -------


def test_ac49_epoch_overflow_forces_full_rebuild_with_exact_reset(tmp_path):
    from code_indexer.storage.shared.hnsw_sync_state import (
        _MAX_EPOCH,
        read_hnsw_sync_state,
    )

    store = FilesystemVectorStore(base_path=tmp_path)
    store.create_collection("coll", vector_size=VECTOR_DIM)
    store.begin_indexing("coll")
    store.upsert_points("coll", [_point("a", "src/a.py", 1)])
    store.end_indexing("coll")

    collection_path = tmp_path / "coll"
    # Bug #1619: hnsw_sync now lives in its own dedicated file, stored at
    # the file's top level directly (no wrapper key).
    sync_file = collection_path / "hnsw_sync_state.json"
    overflowing_epoch = _MAX_EPOCH + 1  # beyond the sane bound -> invalid
    sync_file.write_text(
        json.dumps(
            {
                "schema_version": HNSW_SYNC_SCHEMA_VERSION,
                "mutation_epoch": overflowing_epoch,
                "published_epoch": overflowing_epoch,
                "status": "clean",
                "current_branch": None,
                "layout": "sharded_json",
            }
        )
    )

    # The overflowing value must be rejected outright (fail-safe: "no valid
    # state"), never silently trusted as a legitimate huge epoch.
    assert read_hnsw_sync_state(collection_path) is None

    identity_before = _hnsw_index_identity(collection_path)

    store2 = FilesystemVectorStore(base_path=tmp_path)
    store2.begin_indexing("coll")
    store2.upsert_points("coll", [_point("b", "src/b.py", 2)])
    store2.end_indexing("coll")

    identity_after = _hnsw_index_identity(collection_path)
    assert identity_after != identity_before, "overflow must trigger a real rebuild"

    sync_after = _hnsw_sync(collection_path)
    # Exact reset: a fresh, bounded, mutually-consistent pair -- never the
    # overflowing value carried forward.
    assert sync_after["mutation_epoch"] == 1
    assert sync_after["published_epoch"] == 1
    assert sync_after["status"] == "clean"
    ids = _query_ids(store2, "coll", 1) | _query_ids(store2, "coll", 2)
    assert ids == {"a", "b"}


# --- AC11(a): visible-path-set exclusion alone (no hidden_branches change) -


def test_ac11a_added_point_excluded_from_visible_files_stays_out_of_query(tmp_path):
    """A point tracked as 'added' this session, whose path is simply absent
    from the `visible_files` set passed to set_hnsw_branch_context (NOT via
    any hidden_branches change -- this is the OTHER hiding mechanism, the
    same one the full-rebuild path filters on), must never become queryable
    via the incremental apply path. Distinct from AC11(b) (hidden_branches
    change alone) -- this proves the incremental algorithm also honors
    `session.visible_files` membership, not merely the payload field.
    """
    store = FilesystemVectorStore(base_path=tmp_path)
    store.create_collection("coll", vector_size=VECTOR_DIM)
    store.begin_indexing("coll")
    store.upsert_points("coll", [_point("a", "src/a.py", 1)])
    store.set_hnsw_branch_context("coll", "main", {"src/a.py"})
    store.end_indexing("coll")
    assert _query_ids(store, "coll", 1) == {"a"}

    # Second session, SAME branch: a genuinely NEW point "b" for
    # "src/b.py" is upserted (a real mutation -> tracked in session.added,
    # entering the incremental path), but "src/b.py" is deliberately NOT
    # included in this session's visible_files -- simulating a file that
    # exists/was chunked but is not part of the currently checked-out
    # branch's file set.
    store.begin_indexing("coll")
    store.upsert_points("coll", [_point("b", "src/b.py", 2)])
    store.set_hnsw_branch_context("coll", "main", {"src/a.py"})  # "b" excluded
    store.end_indexing("coll")

    assert "b" not in _query_ids(store, "coll", 2), (
        "a tracked-added point whose path is outside visible_files must "
        "never enter the index via the incremental path"
    )
    assert _query_ids(store, "coll", 1) == {"a"}
