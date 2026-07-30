"""Codex Finding B (Bug #1488): ``scroll_points()`` must paginate by a stable,
LAYOUT-INDEPENDENT *real point-id* cursor -- NEVER a layout-dependent filename
token.

The prior #1488 fix (``test_filesystem_vector_store_1488_scroll_cursor_continuity.py``)
only proved continuity for NON-temporal ids that were deliberately chosen so the
sharded filename stem equals the real point-id (``p000`` ... ``p008``). That
masked the real defect for TEMPORAL collections:

  * Temporal point-ids are ``{project}:commit:{hash}:{j}`` (contain colons).
  * The write path names a temporal vector file ``vector_<sha256(id)[:16]>.json``
    (see ``upsert_points`` / ``generate_hash_prefix``), so the sharded filename
    token is a HEX DIGEST, NOT the point-id.

So under the buggy code the SHARDED_JSON branch orders/cursors by the hex token
while the CHUNKS_DB branch orders/cursors by the real colon-bearing id. When the
CLI fleet migration flips a temporal collection to CHUNKS_DB mid-pagination, the
hex cursor bisects against colon-bearing ids and lands at index 0 -> page 2
duplicates page 1 (``a d a b c ...`` instead of once-each), the exact
duplicate-and-drop class this fix exists to kill.

These tests use REAL filesystem + REAL SQLite (via the real
``consolidate_collection_in_place`` migration), deterministic, no sleeps, no
mocking of the store's own logic.
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
    _SCROLL_CURSOR_PREFIX,
)
from code_indexer.storage.shared.chunk_layout import (
    ChunkLayout,
    resolve_chunk_layout,
)
from code_indexer.storage.shared.collection_migration import (
    consolidate_collection_in_place,
)
from code_indexer.storage.temporal_metadata_store import generate_hash_prefix

VECTOR_SIZE = 64
TEMPORAL_COLLECTION = "code-indexer-temporal-voyage_code_3"

# Six colon-bearing temporal ids. Their real-id sort order (a,b,c,d,e,f) is
# PROVEN (below) to differ from their sha256[:16] hex-token sort order, so a
# filename-token cursor MUST produce a cross-ordering dup/gap after a flip --
# guaranteeing the RED reproduction is deterministic, not luck.
TEMPORAL_IDS = [f"proj:commit:{c * 8}:0" for c in "abcdef"]
ALL_TEMPORAL_IDS = set(TEMPORAL_IDS)


def _make_temporal_points() -> List[Dict]:
    rng = np.random.default_rng(1488)
    points = []
    for i, pid in enumerate(TEMPORAL_IDS):
        v = rng.standard_normal(VECTOR_SIZE)
        v[i % VECTOR_SIZE] += 25.0
        points.append(
            {
                "id": pid,
                "vector": v.astype(np.float64).tolist(),
                "payload": {"path": f"file_{i}.py", "language": "python"},
            }
        )
    return points


def _build_temporal_sharded_collection(base_path: Path) -> FilesystemVectorStore:
    store = FilesystemVectorStore(
        base_path=base_path, use_chunks_db_for_new_collections=False
    )
    store.create_collection(TEMPORAL_COLLECTION, vector_size=VECTOR_SIZE)
    store.begin_indexing(TEMPORAL_COLLECTION)
    store.upsert_points(TEMPORAL_COLLECTION, _make_temporal_points())
    store.end_indexing(TEMPORAL_COLLECTION)
    return store


def test_precondition_hash_order_differs_from_id_order() -> None:
    """Guarantee the RED reproduction is real: the hex-token order must differ
    from the real-id order, otherwise a token cursor would accidentally agree
    with an id cursor and mask the bug."""
    by_id = sorted(TEMPORAL_IDS)
    by_token = sorted(TEMPORAL_IDS, key=generate_hash_prefix)
    assert by_id != by_token, (
        "test ids no longer trigger the cross-ordering bug -- pick new ids"
    )


class TestTemporalCursorSurvivesLayoutFlip:
    def test_temporal_cursor_survives_flip_no_dup_no_gap(self, tmp_path: Path) -> None:
        base_path = tmp_path / "index"
        base_path.mkdir()
        store = _build_temporal_sharded_collection(base_path)
        collection_path = store._get_collection_path(TEMPORAL_COLLECTION)
        assert resolve_chunk_layout(collection_path) == ChunkLayout.SHARDED_JSON
        # Sanity: filenames really are sha256 hex tokens, not the colon ids.
        vector_files = list(collection_path.rglob("vector_*.json"))
        assert len(vector_files) == len(TEMPORAL_IDS)
        for pid in TEMPORAL_IDS:
            assert any(
                f.name == f"vector_{generate_hash_prefix(pid)}.json"
                for f in vector_files
            ), f"expected hash-token filename for {pid}"

        collected: List[str] = []

        # Page 1 under SHARDED_JSON (limit=3).
        page1, cursor = store.scroll_points(
            collection_name=TEMPORAL_COLLECTION, limit=3, with_payload=True
        )
        assert len(page1) == 3
        assert cursor is not None
        collected.extend(p["id"] for p in page1)

        # Concurrent server-mode migration completes: chunks.db built +
        # discriminator flipped + legacy vector_*.json deleted.
        consolidate_collection_in_place(collection_path)
        assert resolve_chunk_layout(collection_path) == ChunkLayout.CHUNKS_DB
        assert not list(collection_path.rglob("vector_*.json"))

        # Remaining pages under CHUNKS_DB, resuming from the SHARDED cursor.
        guard = 0
        while cursor is not None:
            guard += 1
            assert guard <= len(TEMPORAL_IDS) + 2, "pagination did not terminate"
            page, cursor = store.scroll_points(
                collection_name=TEMPORAL_COLLECTION,
                limit=3,
                with_payload=True,
                offset=cursor,
            )
            collected.extend(p["id"] for p in page)

        # No duplicates, no dropped rows, and EXACT once-each ordered traversal
        # (both layouts iterate the SAME sorted real-id order).
        assert len(collected) == len(set(collected)), (
            f"duplicate temporal ids across the flip: {collected}"
        )
        assert set(collected) == ALL_TEMPORAL_IDS, (
            f"paginated temporal view lost/gained rows: {sorted(set(collected))}"
        )
        assert collected == sorted(TEMPORAL_IDS), (
            f"traversal not in stable real-id order: {collected}"
        )


class TestGarbageCursorFailsLoud:
    """Messi #13: an unrecognized/garbage cursor must FAIL LOUD, never silently
    bisect to a wrong position and re-emit page 1."""

    def test_garbage_cursor_raises_on_sharded(self, tmp_path: Path) -> None:
        base_path = tmp_path / "index"
        base_path.mkdir()
        store = _build_temporal_sharded_collection(base_path)
        with pytest.raises(ValueError):
            store.scroll_points(
                collection_name=TEMPORAL_COLLECTION,
                limit=3,
                with_payload=True,
                offset="totally-bogus-cursor",
            )

    def test_garbage_cursor_raises_on_chunks_db(self, tmp_path: Path) -> None:
        base_path = tmp_path / "index"
        base_path.mkdir()
        store = _build_temporal_sharded_collection(base_path)
        collection_path = store._get_collection_path(TEMPORAL_COLLECTION)
        consolidate_collection_in_place(collection_path)
        assert resolve_chunk_layout(collection_path) == ChunkLayout.CHUNKS_DB

        reader = FilesystemVectorStore(
            base_path=base_path, use_chunks_db_for_new_collections=False
        )
        with pytest.raises(ValueError):
            reader.scroll_points(
                collection_name=TEMPORAL_COLLECTION,
                limit=3,
                with_payload=True,
                offset="totally-bogus-cursor",
            )


class TestDeletedButValidIdCursorResumes:
    """A deleted-but-valid *self-describing* id cursor must resume at the
    next-greater id (no dup, no fail) -- distinct from a garbage cursor."""

    def test_deleted_valid_id_cursor_resumes_next_greater(self, tmp_path: Path) -> None:
        base_path = tmp_path / "index"
        base_path.mkdir()
        store = _build_temporal_sharded_collection(base_path)
        collection_path = store._get_collection_path(TEMPORAL_COLLECTION)
        consolidate_collection_in_place(collection_path)
        assert resolve_chunk_layout(collection_path) == ChunkLayout.CHUNKS_DB

        reader = FilesystemVectorStore(
            base_path=base_path, use_chunks_db_for_new_collections=False
        )

        ordered = sorted(TEMPORAL_IDS)
        deleted_id = ordered[2]  # a middle id (proj:commit:cccccccc:0)
        reader.delete_points(TEMPORAL_COLLECTION, [deleted_id])

        # A self-describing cursor for the now-DELETED id: must resume at the
        # first id greater than it -- never fail, never restart at page 1.
        cursor = _SCROLL_CURSOR_PREFIX + deleted_id
        page, _next = reader.scroll_points(
            collection_name=TEMPORAL_COLLECTION,
            limit=10,
            with_payload=True,
            offset=cursor,
        )
        ids = [p["id"] for p in page]
        expected = [pid for pid in ordered if pid > deleted_id]
        assert ids == expected, (
            f"deleted-valid-id cursor did not resume at next-greater: {ids}"
        )
        assert deleted_id not in ids


def _vector_files(collection_path: Path) -> List[Path]:
    return sorted(collection_path.rglob("vector_*.json"))


class TestLegacyScanFailsLoud:
    """Codex MEDIUM (Messi #13): the legacy SHARDED_JSON id-map scan must NOT
    silently skip a PRESENT-but-malformed vector file (bad JSON / missing /
    invalid ``id``) nor silently collapse two files that carry the SAME stored
    id. Both were silent data loss in a read/pagination path: a collection with
    N valid + 1 bad record returned only the valid rows AND a terminal ``None``
    cursor, falsely presenting a complete traversal. Fail loud instead, naming
    the offending file.
    """

    def test_id_less_but_valid_json_raises_naming_file(self, tmp_path: Path) -> None:
        base_path = tmp_path / "index"
        base_path.mkdir()
        store = _build_temporal_sharded_collection(base_path)
        collection_path = store._get_collection_path(TEMPORAL_COLLECTION)
        assert resolve_chunk_layout(collection_path) == ChunkLayout.SHARDED_JSON

        # Corrupt ONE existing vector file into valid JSON that has NO ``id``.
        victim = _vector_files(collection_path)[0]
        victim.write_text(
            json.dumps({"vector": [0.0] * VECTOR_SIZE, "payload": {"path": "x.py"}})
        )

        with pytest.raises(RuntimeError) as exc_info:
            store.scroll_points(
                collection_name=TEMPORAL_COLLECTION, limit=100, with_payload=True
            )
        msg = str(exc_info.value)
        assert victim.name in msg, f"integrity error must name the file: {msg!r}"
        assert "id" in msg

    def test_garbage_json_raises_naming_file(self, tmp_path: Path) -> None:
        base_path = tmp_path / "index"
        base_path.mkdir()
        store = _build_temporal_sharded_collection(base_path)
        collection_path = store._get_collection_path(TEMPORAL_COLLECTION)

        victim = _vector_files(collection_path)[0]
        victim.write_text("this is not json at all {{{{ ][")

        with pytest.raises(RuntimeError) as exc_info:
            store.scroll_points(
                collection_name=TEMPORAL_COLLECTION, limit=100, with_payload=True
            )
        msg = str(exc_info.value)
        assert victim.name in msg, f"integrity error must name the file: {msg!r}"

    def test_duplicate_stored_id_raises_naming_file(self, tmp_path: Path) -> None:
        base_path = tmp_path / "index"
        base_path.mkdir()
        store = _build_temporal_sharded_collection(base_path)
        collection_path = store._get_collection_path(TEMPORAL_COLLECTION)

        # Plant a SECOND vector file (different filename, same shard dir) that
        # carries the SAME stored id as an existing record.
        original = _vector_files(collection_path)[0]
        data = json.loads(original.read_text())
        dup_id = data["id"]
        dup_file = original.parent / "vector_dupdupdupdupdup0.json"
        dup_file.write_text(json.dumps(data))

        with pytest.raises(RuntimeError) as exc_info:
            store.scroll_points(
                collection_name=TEMPORAL_COLLECTION, limit=100, with_payload=True
            )
        msg = str(exc_info.value)
        assert "duplicate" in msg.lower()
        assert dup_id in msg
        assert dup_file.name in msg, f"integrity error must name the file: {msg!r}"


class TestConcurrentFlipFileNotFoundStillRedispatches:
    """Regression companion to the fail-loud scan above: a genuine mid-scan
    FileNotFoundError (a concurrent server-mode fleet migration flipping the
    discriminator + deleting the legacy files in the window between the
    pre-scan resolve and a per-file ``open()``) must STILL trigger the Bug #1486
    Finding-5 CHUNKS_DB re-dispatch -- it must NOT be reclassified as a
    data-integrity error. Only a PRESENT-but-malformed file is an integrity
    error; a VANISHED file is the race.
    """

    def test_mid_open_filenotfound_redispatches_to_chunks_db(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        base_path = tmp_path / "index"
        base_path.mkdir()
        store = _build_temporal_sharded_collection(base_path)
        collection_path = store._get_collection_path(TEMPORAL_COLLECTION)
        assert resolve_chunk_layout(collection_path) == ChunkLayout.SHARDED_JSON

        real_open = builtins.open
        state = {"fired": False}

        def racing_open(file, *args, **kwargs):  # type: ignore[no-untyped-def]
            name = os.path.basename(str(file))
            if (
                not state["fired"]
                and name.startswith("vector_")
                and name.endswith(".json")
            ):
                # The instant the scan opens its FIRST legacy vector file, a
                # real concurrent migration completes: chunks.db built +
                # discriminator flipped + every legacy vector_*.json deleted.
                state["fired"] = True
                consolidate_collection_in_place(collection_path)
            # The real open now hits a file the migration just deleted ->
            # genuine FileNotFoundError (never mocked), inside the scan window.
            return real_open(file, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", racing_open)

        points, next_cursor = store.scroll_points(
            collection_name=TEMPORAL_COLLECTION, limit=100, with_payload=True
        )

        assert state["fired"] is True
        assert resolve_chunk_layout(collection_path) == ChunkLayout.CHUNKS_DB
        assert not list(collection_path.rglob("vector_*.json"))
        # Re-dispatched to chunks.db: the full row set is returned, NOT an
        # integrity raise and NOT an empty/partial page.
        assert {p["id"] for p in points} == ALL_TEMPORAL_IDS, (
            f"concurrent-flip FileNotFoundError did not re-dispatch: {points}"
        )
        assert next_cursor is None


class TestLegacyTokenCursorRejection:
    """Codex LOW: directly exercise the two legacy ``vector_<token>.json`` cursor
    rejection paths (fail loud, Messi #13): a token matching ZERO stored ids, and
    a token matching AMBIGUOUSLY (>1)."""

    def test_zero_match_legacy_token_raises(self) -> None:
        with pytest.raises(ValueError) as exc_info:
            FilesystemVectorStore._resolve_legacy_scroll_token(
                "nomatchtoken", ["proj:commit:aaaa:0", "proj:commit:bbbb:0"]
            )
        assert "no stored" in str(exc_info.value).lower()

    def test_ambiguous_legacy_token_raises(self) -> None:
        # Two distinct stored ids collapse to the SAME slash-replaced token
        # "a_b", so the token matches BOTH -- an unresolvable ambiguity.
        with pytest.raises(ValueError) as exc_info:
            FilesystemVectorStore._resolve_legacy_scroll_token("a_b", ["a/b", "a_b"])
        assert "ambiguous" in str(exc_info.value).lower()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
