"""Two Codex-verified correctness defects in ``FilesystemVectorStore.scroll_points``.

MEDIUM -- PathIndex fast-path evaluated a REDUCED filter (all path clauses
    stripped) instead of the COMPLETE original filter, so:
      * a contradictory multi-path filter (must match a.py AND b.py) wrongly
        returned the a.py candidate row, and
      * a STALE PathIndex entry (indexed path no longer matches the real,
        hydrated payload path) wrongly returned a row whose real path differs
        from the requested path.
    Fix: the PathIndex is used ONLY to obtain candidate ids; the COMPLETE
    original filter (including every path clause) is then evaluated against each
    candidate's REAL hydrated payload -- the same predicate the general rglob
    path uses. A candidate whose real payload doesn't satisfy the full filter is
    dropped, which also self-heals stale PathIndex entries.

LOW -- a SHARDED_JSON vector file whose JSON ROOT is a non-dict (valid JSON
    ``null``, a list, a number, a string) made ``"id" not in data`` raise a raw
    ``TypeError`` instead of the ``ScrollDataIntegrityError`` contract used for
    every other present-but-malformed record. Fix: after BOTH ``json.load()``
    calls in the SHARDED_JSON hydration (the inventory-scan site and the
    per-page hydration site), a ``not isinstance(data, dict)`` guard raises
    ``ScrollDataIntegrityError`` naming the file.

Real filesystem + real SQLite, deterministic, no sleeps, no mocking of the
store's own logic.
"""

import builtins
import json
import os
from pathlib import Path
from typing import Dict, List

import numpy as np
import pytest

from code_indexer.storage.filesystem_vector_store import (
    FilesystemVectorStore,
    PathIndex,
    ScrollDataIntegrityError,
)
from code_indexer.storage.shared.chunk_layout import (
    ChunkLayout,
    resolve_chunk_layout,
)
from code_indexer.storage.shared.collection_dedup_repair import (
    DedupRepairAmbiguousError,
)

VECTOR_SIZE = 32
COLLECTION = "coll"


def _points(spec: Dict[str, str]) -> List[Dict]:
    """spec maps point_id -> path; returns SHARDED-ready point dicts."""
    rng = np.random.default_rng(4242)
    out: List[Dict] = []
    for i, (pid, path) in enumerate(spec.items()):
        v = rng.standard_normal(VECTOR_SIZE)
        v[i % VECTOR_SIZE] += 10.0
        out.append(
            {
                "id": pid,
                "vector": v.astype(np.float64).tolist(),
                "payload": {"path": path, "language": "python"},
            }
        )
    return out


def _build_sharded(base_path: Path, spec: Dict[str, str]) -> FilesystemVectorStore:
    base_path.mkdir(parents=True, exist_ok=True)
    store = FilesystemVectorStore(
        base_path=base_path, use_chunks_db_for_new_collections=False
    )
    store.create_collection(COLLECTION, vector_size=VECTOR_SIZE)
    store.begin_indexing(COLLECTION)
    store.upsert_points(COLLECTION, _points(spec))
    store.end_indexing(COLLECTION)
    return store


def _vector_files(collection_path: Path) -> List[Path]:
    return sorted(collection_path.rglob("vector_*.json"))


def _path_eq(value: str) -> Dict:
    return {"key": "path", "match": {"value": value}}


class TestFastPathEvaluatesFullFilter:
    """MEDIUM: the PathIndex fast-path must evaluate the COMPLETE original
    filter against the real hydrated payload, not a path-stripped subset."""

    def test_contradictory_multi_path_filter_returns_zero_rows(
        self, tmp_path: Path
    ) -> None:
        base_path = tmp_path / "index"
        store = _build_sharded(base_path, {"v0": "a.py", "v1": "b.py"})
        collection_path = store._get_collection_path(COLLECTION)
        assert resolve_chunk_layout(collection_path) == ChunkLayout.SHARDED_JSON

        # {"must": [path==a.py, path==b.py]} is CONTRADICTORY: no single row's
        # path can equal both. The fast-path selects a.py candidates (v0) then
        # must reject v0 because its real payload path (a.py) fails the path==b.py
        # clause. Old behavior stripped both path clauses and returned v0.
        contradictory = {"must": [_path_eq("a.py"), _path_eq("b.py")]}
        points, next_offset = store.scroll_points(
            COLLECTION, limit=10, filter_conditions=contradictory
        )

        assert points == [], (
            f"contradictory multi-path filter must return zero rows, got {points}"
        )
        assert next_offset is None

    def test_single_path_plus_nonpath_clause_still_matches(
        self, tmp_path: Path
    ) -> None:
        # Preservation: a NON-contradictory filter that also carries a non-path
        # clause still returns the matching row (the full-filter evaluation must
        # not over-reject legitimate matches).
        base_path = tmp_path / "index"
        store = _build_sharded(base_path, {"v0": "a.py", "v1": "b.py"})

        good = {
            "must": [
                _path_eq("a.py"),
                {"key": "language", "match": {"value": "python"}},
            ]
        }
        points, _ = store.scroll_points(COLLECTION, limit=10, filter_conditions=good)
        assert [p["id"] for p in points] == ["v0"]

    def test_stale_path_index_entry_excluded_by_full_filter(
        self, tmp_path: Path
    ) -> None:
        base_path = tmp_path / "index"
        # v0's REAL, on-disk payload path is "moved.py".
        store = _build_sharded(base_path, {"v0": "moved.py"})

        # Inject a STALE PathIndex mapping: an OLD path "old.py" still points at
        # v0 (as if the file was renamed on disk but the index wasn't updated).
        stale = PathIndex()
        stale.add_point("old.py", "v0")
        with store._path_index_lock:
            store._path_indexes[COLLECTION] = stale

        # Query the stale path. The fast-path enumerates v0 as a candidate, but
        # v0's real hydrated payload path is "moved.py", so the full path==old.py
        # filter must exclude it -> zero rows (self-healing).
        points, next_offset = store.scroll_points(
            COLLECTION,
            limit=10,
            filter_conditions={"must": [_path_eq("old.py")]},
        )
        assert points == [], (
            f"stale path-index entry must be excluded by full-filter "
            f"re-evaluation against the real payload, got {points}"
        )
        assert next_offset is None


class TestNonDictJsonRootFailsLoud:
    """LOW: a valid-JSON but non-dict ROOT must raise ScrollDataIntegrityError
    naming the file, not a raw TypeError, at BOTH SHARDED_JSON hydration sites."""

    def test_null_root_at_inventory_site_raises(self, tmp_path: Path) -> None:
        base_path = tmp_path / "index"
        store = _build_sharded(base_path, {"v0": "a.py", "v1": "b.py"})
        collection_path = store._get_collection_path(COLLECTION)
        victim = _vector_files(collection_path)[0]
        victim.write_text("null")  # valid JSON, non-dict root

        with pytest.raises((ScrollDataIntegrityError, DedupRepairAmbiguousError)) as exc:
            # No path filter -> the general rglob path (inventory scan opens
            # every file, hitting the non-dict root during id-map build).
            store.scroll_points(COLLECTION, limit=10)
        assert str(victim) in str(exc.value)

    def test_list_root_at_inventory_site_raises(self, tmp_path: Path) -> None:
        base_path = tmp_path / "index"
        store = _build_sharded(base_path, {"v0": "a.py", "v1": "b.py"})
        collection_path = store._get_collection_path(COLLECTION)
        victim = _vector_files(collection_path)[0]
        victim.write_text(json.dumps([1, 2, 3]))  # valid JSON list root

        with pytest.raises((ScrollDataIntegrityError, DedupRepairAmbiguousError)) as exc:
            store.scroll_points(COLLECTION, limit=10)
        assert str(victim) in str(exc.value)

    def test_null_root_at_per_page_hydration_site_raises(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        base_path = tmp_path / "index"
        store = _build_sharded(base_path, {"v0": "a.py", "v1": "b.py", "v2": "c.py"})
        collection_path = store._get_collection_path(COLLECTION)
        assert resolve_chunk_layout(collection_path) == ChunkLayout.SHARDED_JSON

        n_files = len(_vector_files(collection_path))
        real_open = builtins.open
        state = {"vector_opens": 0, "fired": False}

        def racing_open(file, *args, **kwargs):  # type: ignore[no-untyped-def]
            name = os.path.basename(str(file))
            is_vector = name.startswith("vector_") and name.endswith(".json")
            if is_vector:
                state["vector_opens"] += 1
                # The FULL inventory scan (n_files opens) reads valid dict
                # records untouched; fire on the FIRST per-page hydration re-read
                # (open #n_files+1) by rewriting THAT file to a non-dict root
                # just before it is read back. Site 1 already validated it as a
                # dict; only the site-2 guard can catch this.
                if state["vector_opens"] == n_files + 1:
                    Path(file).write_text("null")
                    state["fired"] = True
            return real_open(file, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", racing_open)

        with pytest.raises(ScrollDataIntegrityError):
            store.scroll_points(COLLECTION, limit=100, with_vectors=False)

        assert state["fired"] is True, "the per-page hydration re-read never fired"
