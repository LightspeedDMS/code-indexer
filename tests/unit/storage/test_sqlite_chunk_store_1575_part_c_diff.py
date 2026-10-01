"""TDD tests for Bug #1575 Part C -- ``ChunkStore.update_payload_fields_batch_with_diff()``.

RED phase: every test in this file must FAIL against pre-Part-C
``ChunkStore`` (no ``update_payload_fields_batch_with_diff``/``PayloadChange``).

Real SQLite via ``ChunkStore`` + ``tmp_path`` throughout -- no mocking.
"""

from typing import List, cast

import numpy as np

from code_indexer.storage.sqlite_chunk_store import ChunkStore, PayloadChange

TEST_VECTOR_DIM = 16
_SEED_MODULUS = 1000
_FIXTURE_LINE_START = 1
_FIXTURE_LINE_END = 2


def _make_vector(seed: int = 0) -> List[float]:
    rng = np.random.default_rng(seed)
    return cast(
        List[float], rng.standard_normal(TEST_VECTOR_DIM).astype(np.float32).tolist()
    )


def _seed_point(store, point_id, path="src/x.py", hidden_branches=None):
    store.write_batch(
        [
            {
                "id": point_id,
                "vector": _make_vector(hash(point_id) % _SEED_MODULUS),
                "metadata": {"language": "python", "type": "content"},
                "payload": {
                    "path": path,
                    "line_start": _FIXTURE_LINE_START,
                    "line_end": _FIXTURE_LINE_END,
                    "hidden_branches": hidden_branches or [],
                },
                "chunk_text": "x = 1",
            }
        ]
    )


def test_diff_reports_hidden_branches_change(tmp_path):
    db_path = tmp_path / "chunks.db"
    with ChunkStore(db_path) as store:
        _seed_point(store, "p1", hidden_branches=[])

        changes = store.update_payload_fields_batch_with_diff(
            [("p1", {"hidden_branches": ["feature-x"]})]
        )

    assert len(changes) == 1
    change = changes[0]
    assert isinstance(change, PayloadChange)
    assert change.point_id == "p1"
    assert change.old_hidden_branches == ()
    assert change.new_hidden_branches == ("feature-x",)


def test_diff_reports_path_change(tmp_path):
    db_path = tmp_path / "chunks.db"
    with ChunkStore(db_path) as store:
        _seed_point(store, "p2", path="src/old.py")

        changes = store.update_payload_fields_batch_with_diff(
            [("p2", {"path": "src/new.py"})]
        )

    assert len(changes) == 1
    change = changes[0]
    assert change.old_path == "src/old.py"
    assert change.new_path == "src/new.py"


def test_diff_no_op_merge_reports_identical_old_and_new(tmp_path):
    """A merge that sets the SAME value as already stored is still a valid
    diff entry (old == new) -- the CALLER decides whether that counts as a
    real change (Bug #1575 spec: 'no-op merges are not registered' is the
    CALLER's responsibility when consuming the diff, e.g. only adding to
    visibility_changed when old != new)."""
    db_path = tmp_path / "chunks.db"
    with ChunkStore(db_path) as store:
        _seed_point(store, "p3", hidden_branches=["main"])

        changes = store.update_payload_fields_batch_with_diff(
            [("p3", {"hidden_branches": ["main"]})]
        )

    assert len(changes) == 1
    change = changes[0]
    assert change.old_hidden_branches == ("main",)
    assert change.new_hidden_branches == ("main",)


def test_diff_skips_missing_point_id(tmp_path):
    db_path = tmp_path / "chunks.db"
    with ChunkStore(db_path) as store:
        _seed_point(store, "p4")

        changes = store.update_payload_fields_batch_with_diff(
            [
                ("p4", {"hidden_branches": ["x"]}),
                ("does-not-exist", {"hidden_branches": ["y"]}),
            ]
        )

    assert len(changes) == 1
    assert changes[0].point_id == "p4"


def test_diff_batch_updates_persist_across_fresh_connection(tmp_path):
    """Both updates must be durably committed -- verified by reopening a
    FRESH ChunkStore connection against the same file (never trusting the
    in-memory session that performed the write)."""
    db_path = tmp_path / "chunks.db"
    with ChunkStore(db_path) as store:
        _seed_point(store, "p5", hidden_branches=[])
        _seed_point(store, "p6", hidden_branches=[])

        changes = store.update_payload_fields_batch_with_diff(
            [
                ("p5", {"hidden_branches": ["a"]}),
                ("p6", {"hidden_branches": ["b"]}),
            ]
        )

    assert len(changes) == 2

    with ChunkStore(db_path) as reopened:
        r5 = reopened.read("p5")
        r6 = reopened.read("p6")

    assert r5 is not None and r5["payload"]["hidden_branches"] == ["a"]
    assert r6 is not None and r6["payload"]["hidden_branches"] == ["b"]


def test_diff_empty_updates_returns_empty_list(tmp_path):
    db_path = tmp_path / "chunks.db"
    with ChunkStore(db_path) as store:
        changes = store.update_payload_fields_batch_with_diff([])

    assert changes == []


def test_diff_preserves_vector_untouched(tmp_path):
    db_path = tmp_path / "chunks.db"
    with ChunkStore(db_path) as store:
        _seed_point(store, "p7", hidden_branches=[])
        original = store.read("p7")
        assert original is not None

        store.update_payload_fields_batch_with_diff(
            [("p7", {"hidden_branches": ["z"]})]
        )
        updated = store.read("p7")

    assert updated is not None
    assert (
        np.asarray(updated["vector"], dtype="<f4").tobytes()
        == np.asarray(original["vector"], dtype="<f4").tobytes()
    )
