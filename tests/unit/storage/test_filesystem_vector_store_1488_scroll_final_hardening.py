"""Bug #1488 FINAL scroll_points hardening (Codex): close every remaining
scroll branch that still bypassed the fail-loud / validate-vector /
sorted-id-cursor / filter-against-real-payload guarantees.

Three genuine correctness defects are covered here:

  1. The path-equality PathIndex FAST PATH used to (a) silently skip an
     unhydratable enumerated id, (b) return vectors WITHOUT
     ``_validate_scroll_vector`` under ``with_vectors=True``, and (c) truncate a
     multi-match result with ``result_points[:limit], None`` -- no continuation
     cursor, so with N>limit matching ids the remainder was PERMANENTLY
     unreachable. The fast path must now obey the SAME contract as the general
     paginated path.

  2. BOTH general layouts (CHUNKS_DB and SHARDED_JSON) evaluated the caller's
     payload FILTER against a payload that had been OMITTED (``{}``) whenever
     ``with_payload=False`` -- so a record whose REAL ``payload.language`` matched
     was wrongly dropped from a filtered scroll. The filter must ALWAYS see the
     real hydrated payload; ``with_payload`` controls only what is RETURNED.

  3. The public entry did not reject a non-positive ``limit``.

Real filesystem + real SQLite, deterministic, no sleeps, no mocking of the
store's own logic.
"""

import json
import os
from pathlib import Path
from typing import Dict, List

import numpy as np
import pytest

from code_indexer.storage.filesystem_vector_store import (
    FilesystemVectorStore,
    ScrollDataIntegrityError,
    _SCROLL_CURSOR_PREFIX,
)

VECTOR_SIZE = 32
_PATH_FILTER = {"must": [{"key": "path", "match": {"value": "src/foo.py"}}]}
_LANG_FILTER = {"must": [{"key": "language", "match": {"value": "python"}}]}


def _vec(seed: int) -> List[float]:
    rng = np.random.default_rng(seed)
    return rng.standard_normal(VECTOR_SIZE).astype(np.float64).tolist()  # type: ignore[no-any-return]


def _build_store(base_path: Path, *, chunks_db: bool) -> FilesystemVectorStore:
    return FilesystemVectorStore(
        base_path=base_path, use_chunks_db_for_new_collections=chunks_db
    )


def _index(store: FilesystemVectorStore, coll: str, points: List[Dict]) -> None:
    store.create_collection(coll, vector_size=VECTOR_SIZE)
    store.begin_indexing(coll)
    store.upsert_points(coll, points)
    store.end_indexing(coll)


def _same_path_points() -> List[Dict]:
    # THREE points all sharing ONE path so the path-equality fast path matches
    # all three -- with limit=1 the last two used to be unreachable.
    return [
        {
            "id": f"p{i:03d}",
            "vector": _vec(i),
            "payload": {"path": "src/foo.py", "language": "python"},
        }
        for i in range(3)
    ]


def _mixed_points() -> List[Dict]:
    pts = []
    for i in range(4):
        lang = "python" if i % 2 == 0 else "java"
        pts.append(
            {
                "id": f"q{i:03d}",
                "vector": _vec(100 + i),
                # Distinct paths so the path fast path is NOT triggered.
                "payload": {"path": f"src/f{i}.py", "language": lang},
            }
        )
    return pts


def _sharded_vector_files(store: FilesystemVectorStore, coll: str) -> List[Path]:
    collection_path = store._get_collection_path(coll)
    return [
        f
        for f in collection_path.rglob("vector_*.json")
        if "collection_meta" not in f.name
    ]


# ---------------------------------------------------------------------------
# Defect 1: path-equality fast path -- pagination + fail-loud + validation
# ---------------------------------------------------------------------------


class TestFastPathPaginationAndFailLoud:
    def test_fast_path_multi_match_limit1_is_fully_reachable(self, tmp_path):
        """3 matching ids + limit=1: page 1 returns 1 point AND a non-None
        continuation cursor; paging through returns ALL 3 exactly once."""
        base = tmp_path / "index"
        base.mkdir()
        store = _build_store(base, chunks_db=False)
        _index(store, "col", _same_path_points())

        collected: List[str] = []
        page, cursor = store.scroll_points(
            collection_name="col",
            limit=1,
            with_payload=True,
            filter_conditions=_PATH_FILTER,
        )
        assert len(page) == 1
        assert cursor is not None, "fast path must yield a continuation cursor"
        assert cursor.startswith(_SCROLL_CURSOR_PREFIX)
        collected.extend(p["id"] for p in page)

        # Statically bounded: at most 3 matching ids -> at most 3 more pages.
        for _ in range(3):
            if cursor is None:
                break
            page, cursor = store.scroll_points(
                collection_name="col",
                limit=1,
                with_payload=True,
                offset=cursor,
                filter_conditions=_PATH_FILTER,
            )
            collected.extend(p["id"] for p in page)

        assert cursor is None, "pagination did not terminate within bound"
        assert collected == ["p000", "p001", "p002"], collected
        assert len(collected) == len(set(collected)), "duplicate rows in fast path"

    def test_fast_path_raises_on_malformed_vector(self, tmp_path):
        """with_vectors=True on the fast path must validate the vector and fail
        LOUD on a wrong-dimension stored vector (SHARDED_JSON)."""
        base = tmp_path / "index"
        base.mkdir()
        store = _build_store(base, chunks_db=False)
        _index(store, "col", _same_path_points())

        vfiles = _sharded_vector_files(store, "col")
        assert vfiles, "expected sharded vector files on disk"
        target = vfiles[0]
        data = json.loads(target.read_text())
        data["vector"] = [0.1, 0.2]  # dim 2 != VECTOR_SIZE
        target.write_text(json.dumps(data))

        with pytest.raises(ScrollDataIntegrityError):
            store.scroll_points(
                collection_name="col",
                limit=100,
                with_vectors=True,
                filter_conditions=_PATH_FILTER,
            )

    def test_fast_path_raises_on_unhydratable_id(self, tmp_path):
        """An id enumerated by the PathIndex whose backing point cannot be
        hydrated (file gone) must RAISE, never be silently skipped."""
        base = tmp_path / "index"
        base.mkdir()
        store = _build_store(base, chunks_db=False)
        _index(store, "col", _same_path_points())

        vfiles = _sharded_vector_files(store, "col")
        assert vfiles
        os.unlink(vfiles[0])

        with pytest.raises(ScrollDataIntegrityError):
            store.scroll_points(
                collection_name="col",
                limit=100,
                with_payload=True,
                filter_conditions=_PATH_FILTER,
            )


# ---------------------------------------------------------------------------
# Defect 2: filter must see the REAL payload even when with_payload=False
# ---------------------------------------------------------------------------


class TestFilterVsWithPayloadFalse:
    @pytest.mark.parametrize("chunks_db", [False, True])
    def test_filter_with_payload_false_returns_matches_without_payload(
        self, tmp_path, chunks_db
    ):
        base = tmp_path / "index"
        base.mkdir()
        store = _build_store(base, chunks_db=chunks_db)
        _index(store, "col", _mixed_points())

        points, _ = store.scroll_points(
            collection_name="col",
            limit=100,
            with_payload=False,
            filter_conditions=_LANG_FILTER,
        )
        ids = {p["id"] for p in points}
        assert ids == {"q000", "q002"}, ids
        for p in points:
            assert "payload" not in p, "with_payload=False must omit payload"

    @pytest.mark.parametrize("chunks_db", [False, True])
    def test_filter_with_payload_true_returns_matches_with_payload(
        self, tmp_path, chunks_db
    ):
        base = tmp_path / "index"
        base.mkdir()
        store = _build_store(base, chunks_db=chunks_db)
        _index(store, "col", _mixed_points())

        points, _ = store.scroll_points(
            collection_name="col",
            limit=100,
            with_payload=True,
            filter_conditions=_LANG_FILTER,
        )
        ids = {p["id"] for p in points}
        assert ids == {"q000", "q002"}, ids
        for p in points:
            assert p["payload"]["language"] == "python"


# ---------------------------------------------------------------------------
# Defect 3: non-positive limit
# ---------------------------------------------------------------------------


class TestNonPositiveLimit:
    @pytest.mark.parametrize("bad_limit", [0, -1, -100])
    def test_non_positive_limit_raises_value_error(self, tmp_path, bad_limit):
        base = tmp_path / "index"
        base.mkdir()
        store = _build_store(base, chunks_db=False)
        _index(
            store,
            "col",
            [
                {
                    "id": "z000",
                    "vector": _vec(7),
                    "payload": {"path": "src/z.py", "language": "python"},
                }
            ],
        )
        with pytest.raises(ValueError):
            store.scroll_points(collection_name="col", limit=bad_limit)
