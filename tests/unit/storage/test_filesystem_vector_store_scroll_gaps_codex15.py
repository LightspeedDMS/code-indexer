"""Three Codex-15 correctness findings in ``FilesystemVectorStore.scroll_points``
and ``get_point``.

MEDIUM (regression) -- the filtered scroll paths sliced candidates by ``limit``
    BEFORE evaluating the filter, so a page whose sliced candidates all failed
    the filter returned ``points=[]`` with a NON-null continuation cursor. Real
    callers treat an empty page as TERMINAL, so still-matching rows past the
    slice were permanently DROPPED. Fix: iterate candidates in sorted order
    until either ``limit`` MATCHING rows are collected OR candidates are
    exhausted; base the continuation cursor on the LAST EXAMINED candidate, so a
    page is empty ONLY when no further matches exist (then the cursor is None /
    terminal). Applies to all three filtered branches: the PathIndex fast path,
    the general SHARDED_JSON path, and the CHUNKS_DB path.

MEDIUM -- a real ``multimodal_index/<coll>`` nested collection could not be
    scrolled: existence check + collection-path resolution ignored the
    subdirectory, so ``scroll_points`` returned ``([], None)``. Fix: thread the
    subdirectory (explicit param, falling back to the active-indexing
    subdirectory) through the existence check, PathIndex lookup, metadata read,
    and both layout branches.

LOW -- ``get_point``'s SHARDED_JSON reader swallowed JSONDecodeError/KeyError
    and did an unguarded ``data.get()`` so a non-dict JSON root raised a raw
    ``AttributeError`` instead of the ``ScrollDataIntegrityError`` fail-loud
    contract. Fix: a present-but-malformed record (bad JSON / non-dict root /
    missing id or vector) raises ``ScrollDataIntegrityError`` naming the file;
    FileNotFoundError stays the vanished-file signal (returns None).

Real filesystem + real SQLite, deterministic, no sleeps, no mocking of the
store's own logic.
"""

from pathlib import Path
from typing import Dict, List, Optional, Tuple

import numpy as np
import pytest

from code_indexer.storage.filesystem_vector_store import (
    FilesystemVectorStore,
    ScrollDataIntegrityError,
)
from code_indexer.storage.shared.chunk_layout import (
    ChunkLayout,
    resolve_chunk_layout,
)

VECTOR_SIZE = 32
COLLECTION = "coll"


def _points(spec: List[Tuple[str, str, str]]) -> List[Dict]:
    """spec is a list of (point_id, path, language) tuples."""
    rng = np.random.default_rng(4242)
    out: List[Dict] = []
    for i, (pid, path, language) in enumerate(spec):
        v = rng.standard_normal(VECTOR_SIZE)
        v[i % VECTOR_SIZE] += 10.0
        out.append(
            {
                "id": pid,
                "vector": v.astype(np.float64).tolist(),
                "payload": {"path": path, "language": language},
            }
        )
    return out


def _build(
    base_path: Path,
    spec: List[Tuple[str, str, str]],
    *,
    chunks_db: bool = False,
    subdirectory: Optional[str] = None,
) -> FilesystemVectorStore:
    base_path.mkdir(parents=True, exist_ok=True)
    store = FilesystemVectorStore(
        base_path=base_path, use_chunks_db_for_new_collections=chunks_db
    )
    store.create_collection(
        COLLECTION, vector_size=VECTOR_SIZE, subdirectory=subdirectory
    )
    store.begin_indexing(COLLECTION, subdirectory=subdirectory)
    store.upsert_points(COLLECTION, _points(spec))
    store.end_indexing(COLLECTION, subdirectory=subdirectory)
    return store


def _path_eq(value: str) -> Dict:
    return {"key": "path", "match": {"value": value}}


def _lang_eq(value: str) -> Dict:
    return {"key": "language", "match": {"value": value}}


def _paginate_all(
    store: FilesystemVectorStore,
    filter_conditions: Optional[Dict],
    limit: int,
    *,
    subdirectory: Optional[str] = None,
) -> Tuple[List[str], List[Optional[str]]]:
    """Paginate to exhaustion. Returns (all_ids_seen, offsets_of_empty_pages)."""
    seen: List[str] = []
    empty_page_offsets: List[Optional[str]] = []
    offset: Optional[str] = None
    for _ in range(1000):
        pts, offset = store.scroll_points(
            COLLECTION,
            limit=limit,
            filter_conditions=filter_conditions,
            offset=offset,
            subdirectory=subdirectory,
        )
        if not pts:
            empty_page_offsets.append(offset)
        seen.extend(p["id"] for p in pts)
        if offset is None:
            break
    return seen, empty_page_offsets


class TestFastPathIterateUntilLimitMatches:
    """MEDIUM regression: the PathIndex fast path must not drop matching rows
    behind a limit-slice that only contained non-matching candidates."""

    def test_codex_exact_case_first_page_returns_the_match_not_empty(
        self, tmp_path: Path
    ) -> None:
        base_path = tmp_path / "index"
        # p000 and p001 share path a.py; only p001 is python.
        store = _build(
            base_path,
            [("p000", "a.py", "java"), ("p001", "a.py", "python")],
        )
        collection_path = store._get_collection_path(COLLECTION)
        assert resolve_chunk_layout(collection_path) == ChunkLayout.SHARDED_JSON

        # path==a.py triggers the fast path; language==python is the extra
        # clause that rejects p000. limit=1.
        filt = {"must": [_path_eq("a.py"), _lang_eq("python")]}
        points, next_offset = store.scroll_points(
            COLLECTION, limit=1, filter_conditions=filt
        )

        assert [p["id"] for p in points] == ["p001"], (
            f"first page must return the matching row p001, not an empty "
            f"non-terminal page, got {points}"
        )
        assert next_offset is None

    def test_full_pagination_returns_all_matches_no_dropped_rows(
        self, tmp_path: Path
    ) -> None:
        base_path = tmp_path / "index"
        store = _build(
            base_path,
            [
                ("p000", "a.py", "java"),
                ("p001", "a.py", "python"),
                ("p002", "a.py", "python"),
            ],
        )
        filt = {"must": [_path_eq("a.py"), _lang_eq("python")]}
        seen, empty_offsets = _paginate_all(store, filt, limit=1)

        assert seen == ["p001", "p002"], f"dropped a matching row: {seen}"
        assert len(seen) == len(set(seen)), "duplicate row across pages"
        # An empty page is allowed ONLY at the true end (offset None / terminal).
        assert all(off is None for off in empty_offsets), (
            f"an empty page was emitted with a non-terminal cursor: {empty_offsets}"
        )


class TestGeneralShardedJsonIterateUntilLimit:
    """MEDIUM regression: the general SHARDED_JSON path (non-path filter, so the
    fast path is not taken) must iterate-until-limit-matches too."""

    def test_first_page_returns_match_not_empty(self, tmp_path: Path) -> None:
        base_path = tmp_path / "index"
        store = _build(
            base_path,
            [("p000", "a.py", "java"), ("p001", "b.py", "python")],
        )
        collection_path = store._get_collection_path(COLLECTION)
        assert resolve_chunk_layout(collection_path) == ChunkLayout.SHARDED_JSON

        # language-only filter -> no path clause -> general path (not fast path).
        filt = {"must": [_lang_eq("python")]}
        points, next_offset = store.scroll_points(
            COLLECTION, limit=1, filter_conditions=filt
        )
        assert [p["id"] for p in points] == ["p001"], (
            f"general SHARDED_JSON path dropped the match, got {points}"
        )
        assert next_offset is None

    def test_full_pagination_no_dropped_rows(self, tmp_path: Path) -> None:
        base_path = tmp_path / "index"
        store = _build(
            base_path,
            [
                ("p000", "a.py", "java"),
                ("p001", "b.py", "python"),
                ("p002", "c.py", "java"),
                ("p003", "d.py", "python"),
            ],
        )
        filt = {"must": [_lang_eq("python")]}
        seen, empty_offsets = _paginate_all(store, filt, limit=1)
        assert seen == ["p001", "p003"], f"dropped a matching row: {seen}"
        assert all(off is None for off in empty_offsets), empty_offsets


class TestChunksDbIterateUntilLimit:
    """MEDIUM regression: the CHUNKS_DB path must iterate-until-limit-matches."""

    def test_first_page_returns_match_not_empty(self, tmp_path: Path) -> None:
        base_path = tmp_path / "index"
        store = _build(
            base_path,
            [("p000", "a.py", "java"), ("p001", "b.py", "python")],
            chunks_db=True,
        )
        collection_path = store._get_collection_path(COLLECTION)
        assert resolve_chunk_layout(collection_path) == ChunkLayout.CHUNKS_DB

        filt = {"must": [_lang_eq("python")]}
        points, next_offset = store.scroll_points(
            COLLECTION, limit=1, filter_conditions=filt
        )
        assert [p["id"] for p in points] == ["p001"], (
            f"CHUNKS_DB path dropped the match, got {points}"
        )
        assert next_offset is None

    def test_full_pagination_no_dropped_rows(self, tmp_path: Path) -> None:
        base_path = tmp_path / "index"
        store = _build(
            base_path,
            [
                ("p000", "a.py", "java"),
                ("p001", "b.py", "python"),
                ("p002", "c.py", "java"),
                ("p003", "d.py", "python"),
            ],
            chunks_db=True,
        )
        filt = {"must": [_lang_eq("python")]}
        seen, empty_offsets = _paginate_all(store, filt, limit=1)
        assert seen == ["p001", "p003"], f"dropped a matching row: {seen}"
        assert all(off is None for off in empty_offsets), empty_offsets


class TestNestedMultimodalScroll:
    """MEDIUM: a real multimodal_index/<coll> collection must be scrollable."""

    def test_scroll_returns_rows_for_nested_multimodal_collection(
        self, tmp_path: Path
    ) -> None:
        base_path = tmp_path / "index"
        store = _build(
            base_path,
            [("p000", "a.py", "java"), ("p001", "b.py", "python")],
            subdirectory="multimodal_index",
        )
        # Sanity: files really live under the nested subdirectory.
        nested = base_path / "multimodal_index" / COLLECTION
        assert (nested / "collection_meta.json").exists()

        points, next_offset = store.scroll_points(
            COLLECTION, limit=100, subdirectory="multimodal_index"
        )
        assert sorted(p["id"] for p in points) == ["p000", "p001"], (
            f"nested multimodal collection returned no/incorrect rows: {points}"
        )
        assert next_offset is None

    def test_scroll_filtered_nested_multimodal_collection(self, tmp_path: Path) -> None:
        base_path = tmp_path / "index"
        store = _build(
            base_path,
            [("p000", "a.py", "java"), ("p001", "b.py", "python")],
            subdirectory="multimodal_index",
        )
        filt = {"must": [_lang_eq("python")]}
        points, _ = store.scroll_points(
            COLLECTION,
            limit=10,
            filter_conditions=filt,
            subdirectory="multimodal_index",
        )
        assert [p["id"] for p in points] == ["p001"]


class TestGetPointFailLoud:
    """LOW: get_point's SHARDED_JSON reader must fail loud (not raw
    AttributeError / silently None) on a present-but-malformed record."""

    def _victim_file(self, store: FilesystemVectorStore, point_id: str) -> Path:
        with store._id_index_lock:
            if COLLECTION not in store._id_index:
                store._id_index[COLLECTION] = store._load_id_index(COLLECTION)
            vf = store._id_index[COLLECTION].get(point_id)
        assert vf is not None and vf.exists()
        return vf  # type: ignore[no-any-return]

    def test_null_json_root_raises_naming_file(self, tmp_path: Path) -> None:
        base_path = tmp_path / "index"
        store = _build(base_path, [("p000", "a.py", "python")])
        victim = self._victim_file(store, "p000")
        victim.write_text("null")  # valid JSON, non-dict root

        with pytest.raises(ScrollDataIntegrityError) as exc:
            store.get_point("p000", COLLECTION)
        assert str(victim) in str(exc.value)

    def test_list_json_root_raises_naming_file(self, tmp_path: Path) -> None:
        base_path = tmp_path / "index"
        store = _build(base_path, [("p000", "a.py", "python")])
        victim = self._victim_file(store, "p000")
        victim.write_text("[1, 2, 3]")  # valid JSON list root

        with pytest.raises(ScrollDataIntegrityError) as exc:
            store.get_point("p000", COLLECTION)
        assert str(victim) in str(exc.value)

    def test_bad_json_raises_naming_file(self, tmp_path: Path) -> None:
        base_path = tmp_path / "index"
        store = _build(base_path, [("p000", "a.py", "python")])
        victim = self._victim_file(store, "p000")
        victim.write_text("{not valid json")

        with pytest.raises(ScrollDataIntegrityError) as exc:
            store.get_point("p000", COLLECTION)
        assert str(victim) in str(exc.value)

    def test_vanished_file_returns_none(self, tmp_path: Path) -> None:
        base_path = tmp_path / "index"
        store = _build(base_path, [("p000", "a.py", "python")])
        victim = self._victim_file(store, "p000")
        victim.unlink()  # vanished -> not a corruption signal

        assert store.get_point("p000", COLLECTION) is None

    def test_missing_point_returns_none(self, tmp_path: Path) -> None:
        base_path = tmp_path / "index"
        store = _build(base_path, [("p000", "a.py", "python")])
        assert store.get_point("does-not-exist", COLLECTION) is None
