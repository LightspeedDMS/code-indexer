"""Codex follow-up (Bug #1488, Messi #13 anti-silent-failure): two REMAINING
fail-loud gaps in ``scroll_points()`` that the prior scan-level hardening did
NOT cover.

HIGH -- malformed legacy record silently omitted during pagination HYDRATION:
    The SHARDED_JSON id-map scan validates only the ``id`` field, so a valid-JSON
    record with a nonempty ``id`` but a MISSING ``vector`` is accepted into the
    inventory. During per-page hydration with ``with_vectors=True`` the code
    accesses ``data["vector"]`` inside a block whose ``except (JSONDecodeError,
    KeyError): continue`` SILENTLY drops the record -- it vanishes from the
    scroll result, potentially with a terminal ``None`` cursor falsely presenting
    a complete traversal. Must instead RAISE ``ScrollDataIntegrityError`` naming
    the file AND the missing field. CRITICAL: a genuinely VANISHED file
    (concurrent migration flip+delete, the Bug #1486 race) still raises
    ``FileNotFoundError`` and must STILL re-dispatch to chunks.db -- never be
    reclassified as an integrity error.

MEDIUM -- empty prefix-only cursor silently restarts pagination:
    ``__cidx_scroll_v1__:`` strips to an EMPTY embedded point-id; both scroll
    branches then ``bisect_right(ids, "")`` -> index 0 -> silent restart at page
    1 (duplicates already-consumed results). Must RAISE ``ValueError`` (fail
    loud) after stripping when the embedded id is empty, on BOTH branches.

Real filesystem + real SQLite (via the real ``consolidate_collection_in_place``
migration), deterministic, no sleeps, no mocking of the store's own logic.
"""

import builtins
import json
import os
import sqlite3
from pathlib import Path
from typing import Dict, List

import numpy as np
import pytest

import code_indexer.storage.sqlite_chunk_store as _scs_module
from code_indexer.storage.filesystem_vector_store import (
    FilesystemVectorStore,
    ScrollDataIntegrityError,
    _SCROLL_CURSOR_PREFIX,
)
from code_indexer.storage.shared.chunk_layout import (
    ChunkLayout,
    resolve_chunk_layout,
)
from code_indexer.storage.shared.collection_migration import (
    consolidate_collection_in_place,
)

VECTOR_SIZE = 64
TEMPORAL_COLLECTION = "code-indexer-temporal-voyage_code_3"
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


def _vector_files(collection_path: Path) -> List[Path]:
    return sorted(collection_path.rglob("vector_*.json"))


class TestMissingVectorHydrationFailsLoud:
    """HIGH: a present, valid-JSON legacy record with a nonempty ``id`` but a
    MISSING ``vector`` must RAISE ``ScrollDataIntegrityError`` during hydration
    (with_vectors=True) -- never be silently dropped from the page."""

    def test_missing_vector_with_vectors_true_raises_naming_file_and_field(
        self, tmp_path: Path
    ) -> None:
        base_path = tmp_path / "index"
        base_path.mkdir()
        store = _build_temporal_sharded_collection(base_path)
        collection_path = store._get_collection_path(TEMPORAL_COLLECTION)
        assert resolve_chunk_layout(collection_path) == ChunkLayout.SHARDED_JSON

        # Corrupt ONE existing vector file into valid JSON with a nonempty id
        # but NO ``vector`` field -- passes the id-only scan, then would be
        # silently dropped during hydration under the old KeyError-swallow.
        victim = _vector_files(collection_path)[0]
        original = json.loads(victim.read_text())
        victim.write_text(
            json.dumps({"id": original["id"], "payload": {"path": "x.py"}})
        )

        with pytest.raises(ScrollDataIntegrityError) as exc_info:
            store.scroll_points(
                collection_name=TEMPORAL_COLLECTION,
                limit=100,
                with_payload=True,
                with_vectors=True,
            )
        msg = str(exc_info.value)
        assert victim.name in msg, f"integrity error must name the file: {msg!r}"
        assert "vector" in msg, f"integrity error must name the field: {msg!r}"

    def test_missing_vector_with_vectors_false_does_not_raise(
        self, tmp_path: Path
    ) -> None:
        """No happy-path behavior change: when ``with_vectors=False`` the vector
        field is never accessed, so a vector-less record is NOT an integrity
        fault -- the scroll returns normally."""
        base_path = tmp_path / "index"
        base_path.mkdir()
        store = _build_temporal_sharded_collection(base_path)
        collection_path = store._get_collection_path(TEMPORAL_COLLECTION)

        victim = _vector_files(collection_path)[0]
        original = json.loads(victim.read_text())
        victim.write_text(
            json.dumps({"id": original["id"], "payload": {"path": "x.py"}})
        )

        points, next_cursor = store.scroll_points(
            collection_name=TEMPORAL_COLLECTION,
            limit=100,
            with_payload=True,
            with_vectors=False,
        )
        # All ids present (nothing silently dropped) and traversal complete.
        assert {p["id"] for p in points} == ALL_TEMPORAL_IDS
        assert next_cursor is None


class TestConcurrentFlipDuringHydrationStillRedispatches:
    """Regression companion to the HIGH fix: a genuine FileNotFoundError raised
    during the per-page HYDRATION re-read (not the scan) -- a concurrent
    migration flipping the discriminator + deleting the legacy files after the
    scan completed -- must STILL trigger the Bug #1486 Finding-5 CHUNKS_DB
    re-dispatch, NOT be reclassified as a data-integrity error."""

    def test_mid_hydration_filenotfound_redispatches_to_chunks_db(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        base_path = tmp_path / "index"
        base_path.mkdir()
        store = _build_temporal_sharded_collection(base_path)
        collection_path = store._get_collection_path(TEMPORAL_COLLECTION)
        assert resolve_chunk_layout(collection_path) == ChunkLayout.SHARDED_JSON

        n_files = len(_vector_files(collection_path))
        real_open = builtins.open
        state = {"vector_opens": 0, "fired": False}

        def racing_open(file, *args, **kwargs):  # type: ignore[no-untyped-def]
            name = os.path.basename(str(file))
            is_vector = name.startswith("vector_") and name.endswith(".json")
            if is_vector and not state["fired"]:
                state["vector_opens"] += 1
                # Let the FULL scan (n_files opens) succeed untouched; fire only
                # on the FIRST hydration re-read (open #n_files+1). At that point
                # a real concurrent migration completes: chunks.db built +
                # discriminator flipped + every legacy vector_*.json deleted.
                if state["vector_opens"] == n_files + 1:
                    state["fired"] = True
                    consolidate_collection_in_place(collection_path)
            # The real open now hits a file the migration just deleted ->
            # genuine FileNotFoundError (never mocked), inside the hydration loop.
            return real_open(file, *args, **kwargs)

        monkeypatch.setattr(builtins, "open", racing_open)

        points, next_cursor = store.scroll_points(
            collection_name=TEMPORAL_COLLECTION,
            limit=100,
            with_payload=True,
            with_vectors=True,
        )

        assert state["fired"] is True, "the hydration-window vanish never fired"
        assert resolve_chunk_layout(collection_path) == ChunkLayout.CHUNKS_DB
        assert not list(collection_path.rglob("vector_*.json"))
        # Re-dispatched to chunks.db: the full row set is returned, NOT an
        # integrity raise and NOT an empty/partial page.
        assert {p["id"] for p in points} == ALL_TEMPORAL_IDS, (
            f"concurrent-flip FileNotFoundError did not re-dispatch: {points}"
        )
        assert next_cursor is None


class TestEmptyPrefixOnlyCursorFailsLoud:
    """MEDIUM: a prefix-ONLY cursor ``__cidx_scroll_v1__:`` strips to an empty
    embedded point-id. It must RAISE ``ValueError`` (never bisect on "" and
    silently restart at page 1) on the shared resolver AND on BOTH scroll
    branches."""

    def test_empty_cursor_resolver_raises(self) -> None:
        with pytest.raises(ValueError) as exc_info:
            FilesystemVectorStore._resolve_scroll_cursor(
                _SCROLL_CURSOR_PREFIX, ["proj:commit:aaaa:0", "proj:commit:bbbb:0"]
            )
        assert "empty" in str(exc_info.value).lower()

    def test_empty_cursor_raises_on_sharded(self, tmp_path: Path) -> None:
        base_path = tmp_path / "index"
        base_path.mkdir()
        store = _build_temporal_sharded_collection(base_path)
        collection_path = store._get_collection_path(TEMPORAL_COLLECTION)
        assert resolve_chunk_layout(collection_path) == ChunkLayout.SHARDED_JSON

        with pytest.raises(ValueError):
            store.scroll_points(
                collection_name=TEMPORAL_COLLECTION,
                limit=3,
                with_payload=True,
                offset=_SCROLL_CURSOR_PREFIX,
            )

    def test_empty_cursor_raises_on_chunks_db(self, tmp_path: Path) -> None:
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
                offset=_SCROLL_CURSOR_PREFIX,
            )


def _corrupt_first_vector_field(collection_path: Path, new_vector) -> Path:
    """Rewrite the FIRST legacy vector file so its ``vector`` field is PRESENT
    but set to ``new_vector`` (a malformed value), keeping a nonempty ``id`` so
    the record passes the id-only scan and reaches per-page hydration.

    Uses ``json.dumps`` (which serializes ``float('nan')``/``float('inf')`` as the
    non-strict ``NaN``/``Infinity`` tokens that the production ``json.load`` reads
    back), so NaN/Inf cases round-trip deterministically with no mocking.
    """
    victim = _vector_files(collection_path)[0]
    original = json.loads(victim.read_text())
    victim.write_text(
        json.dumps(
            {"id": original["id"], "vector": new_vector, "payload": {"path": "x.py"}}
        )
    )
    return victim


class TestMalformedVectorHydrationFailsLoud:
    """Codex ITEM 1 tail: a present-but-MALFORMED ``vector`` field (not merely a
    missing one) must ALSO RAISE ``ScrollDataIntegrityError`` naming the file AND
    the ``vector`` field during hydration (with_vectors=True) -- never be
    silently returned as a wrong value (e.g. ``None``, a string, a NaN)."""

    @pytest.mark.parametrize(
        "bad_vector",
        [
            pytest.param(None, id="null"),
            pytest.param("not a list", id="string"),
            pytest.param({"x": 1}, id="object"),
            pytest.param([], id="empty-list"),
            pytest.param(5, id="scalar-int"),
            pytest.param(1.5, id="scalar-float"),
            pytest.param([0.1, "x", 0.2], id="non-numeric-element"),
            pytest.param([0.1, float("nan"), 0.2], id="nan"),
            pytest.param([0.1, float("inf"), 0.2], id="inf"),
            pytest.param([0.1, float("-inf"), 0.2], id="neg-inf"),
            pytest.param([0.1, 0.2, 0.3, 0.4], id="wrong-dimension"),
            # 2-D nested numeric: np.asarray -> shape (VECTOR_SIZE, 2); numeric,
            # finite, and shape[0] == VECTOR_SIZE == expected_dim, so it slips
            # past a shape[0]-only check. Must be rejected by an ndim == 1 gate.
            pytest.param([[0.1, 0.2]] * VECTOR_SIZE, id="two-dim-nested"),
            # Ragged nested list: np.asarray itself raises a raw ValueError; must
            # be translated into a contextual ScrollDataIntegrityError.
            pytest.param([[0.1, 0.2], [0.3]], id="ragged-nested"),
        ],
    )
    def test_malformed_vector_with_vectors_true_raises_naming_file_and_field(
        self, tmp_path: Path, bad_vector
    ) -> None:
        base_path = tmp_path / "index"
        base_path.mkdir()
        store = _build_temporal_sharded_collection(base_path)
        collection_path = store._get_collection_path(TEMPORAL_COLLECTION)
        assert resolve_chunk_layout(collection_path) == ChunkLayout.SHARDED_JSON

        victim = _corrupt_first_vector_field(collection_path, bad_vector)

        with pytest.raises(ScrollDataIntegrityError) as exc_info:
            store.scroll_points(
                collection_name=TEMPORAL_COLLECTION,
                limit=100,
                with_payload=True,
                with_vectors=True,
            )
        msg = str(exc_info.value)
        assert victim.name in msg, f"integrity error must name the file: {msg!r}"
        assert "vector" in msg, f"integrity error must name the field: {msg!r}"

    @pytest.mark.parametrize(
        "bad_vector",
        [
            pytest.param(None, id="null"),
            pytest.param("not a list", id="string"),
            pytest.param([0.1, float("nan"), 0.2], id="nan"),
            pytest.param([0.1, 0.2], id="wrong-dimension"),
        ],
    )
    def test_malformed_vector_with_vectors_false_does_not_raise(
        self, tmp_path: Path, bad_vector
    ) -> None:
        """Preservation: ``with_vectors=False`` never accesses/validates the
        vector, so a malformed-vector record is NOT an integrity fault -- the
        scroll returns normally with every id present."""
        base_path = tmp_path / "index"
        base_path.mkdir()
        store = _build_temporal_sharded_collection(base_path)
        collection_path = store._get_collection_path(TEMPORAL_COLLECTION)

        _corrupt_first_vector_field(collection_path, bad_vector)

        points, next_cursor = store.scroll_points(
            collection_name=TEMPORAL_COLLECTION,
            limit=100,
            with_payload=True,
            with_vectors=False,
        )
        assert {p["id"] for p in points} == ALL_TEMPORAL_IDS
        assert next_cursor is None

    def test_well_formed_vectors_scroll_normally_with_vectors_true(
        self, tmp_path: Path
    ) -> None:
        """Preservation: an untouched, well-formed collection scrolls the full
        row set with correctly-dimensioned float vectors under
        ``with_vectors=True`` -- validation adds no false positives."""
        base_path = tmp_path / "index"
        base_path.mkdir()
        store = _build_temporal_sharded_collection(base_path)

        points, next_cursor = store.scroll_points(
            collection_name=TEMPORAL_COLLECTION,
            limit=100,
            with_payload=True,
            with_vectors=True,
        )
        assert {p["id"] for p in points} == ALL_TEMPORAL_IDS
        assert next_cursor is None
        for p in points:
            assert isinstance(p["vector"], list)
            assert len(p["vector"]) == VECTOR_SIZE
            assert all(isinstance(x, (int, float)) for x in p["vector"])


def _build_temporal_chunks_db_collection(
    base_path: Path,
) -> tuple["FilesystemVectorStore", Path]:
    """Build a real SHARDED_JSON temporal collection, then consolidate it into a
    real CHUNKS_DB collection via the production migration. Returns a FRESH reader
    store (no in-session build intent) plus the collection path, so the CHUNKS_DB
    branch is reached purely through the committed discriminator."""
    store = _build_temporal_sharded_collection(base_path)
    collection_path = store._get_collection_path(TEMPORAL_COLLECTION)
    consolidate_collection_in_place(collection_path)
    assert resolve_chunk_layout(collection_path) == ChunkLayout.CHUNKS_DB
    reader = FilesystemVectorStore(
        base_path=base_path, use_chunks_db_for_new_collections=False
    )
    return reader, collection_path


def _overwrite_chunks_db_vector(
    collection_path: Path, point_id: str, blob: bytes
) -> None:
    """Directly overwrite ONE row's ``vector`` BLOB in chunks.db with a corrupt
    float32 byte string (a value the write path's validation would never accept),
    keeping the primary key + data intact so the row still hydrates through
    ``ChunkStore.read`` and reaches the scroll's vector-validation gate."""
    conn = sqlite3.connect(str(collection_path / "chunks.db"))
    try:
        conn.execute(
            "UPDATE chunks SET vector = ? WHERE point_id = ?", (blob, point_id)
        )
        conn.commit()
    finally:
        conn.close()


def _nan_blob() -> bytes:
    v = np.arange(VECTOR_SIZE, dtype="<f4")
    v[3] = np.nan
    return v.tobytes()


def _inf_blob() -> bytes:
    v = np.arange(VECTOR_SIZE, dtype="<f4")
    v[3] = np.inf
    return v.tobytes()


class TestChunksDbMalformedVectorHydrationFailsLoud:
    """CHUNKS_DB parity with the SHARDED_JSON hardening: a structurally-valid
    SQLite row whose stored vector decodes to a wrong-dimension, 1-element, or
    non-finite (NaN/Inf) array must RAISE ``ScrollDataIntegrityError`` naming the
    point-id AND the ``vector`` field under ``with_vectors=True`` -- never be
    returned silently. Reuses the SAME ``_validate_scroll_vector`` the
    SHARDED_JSON path uses (no divergent validator)."""

    @pytest.mark.parametrize(
        "bad_blob,label",
        [
            pytest.param(
                np.array([0.1, 0.2, 0.3, 0.4], dtype="<f4").tobytes(),
                "wrong-dimension",
                id="wrong-dimension",
            ),
            pytest.param(
                np.array([0.1], dtype="<f4").tobytes(),
                "one-element",
                id="one-element",
            ),
            pytest.param(b"", "empty", id="empty"),
            pytest.param(_nan_blob(), "nan", id="nan"),
            pytest.param(_inf_blob(), "inf", id="inf"),
        ],
    )
    def test_malformed_chunks_db_vector_with_vectors_true_raises_naming_id_and_field(
        self, tmp_path: Path, bad_blob: bytes, label: str
    ) -> None:
        base_path = tmp_path / "index"
        base_path.mkdir()
        reader, collection_path = _build_temporal_chunks_db_collection(base_path)

        victim_id = TEMPORAL_IDS[0]
        _overwrite_chunks_db_vector(collection_path, victim_id, bad_blob)

        with pytest.raises(ScrollDataIntegrityError) as exc_info:
            reader.scroll_points(
                collection_name=TEMPORAL_COLLECTION,
                limit=100,
                with_payload=True,
                with_vectors=True,
            )
        msg = str(exc_info.value)
        assert victim_id in msg, f"integrity error must name the point-id: {msg!r}"
        assert "vector" in msg, f"integrity error must name the field: {msg!r}"

    @pytest.mark.parametrize(
        "bad_blob",
        [
            pytest.param(
                np.array([0.1, 0.2, 0.3, 0.4], dtype="<f4").tobytes(),
                id="wrong-dimension",
            ),
            pytest.param(_nan_blob(), id="nan"),
        ],
    )
    def test_malformed_chunks_db_vector_with_vectors_false_does_not_raise(
        self, tmp_path: Path, bad_blob: bytes
    ) -> None:
        """Preservation: ``with_vectors=False`` never returns/validates the
        vector, so a corrupt stored vector is NOT an integrity fault -- the scroll
        returns normally with every id present (no expected_dim lookup, no
        raise)."""
        base_path = tmp_path / "index"
        base_path.mkdir()
        reader, collection_path = _build_temporal_chunks_db_collection(base_path)

        _overwrite_chunks_db_vector(collection_path, TEMPORAL_IDS[0], bad_blob)

        points, next_cursor = reader.scroll_points(
            collection_name=TEMPORAL_COLLECTION,
            limit=100,
            with_payload=True,
            with_vectors=False,
        )
        assert {p["id"] for p in points} == ALL_TEMPORAL_IDS
        assert next_cursor is None


class TestChunksDbUnhydratableIdFailsLoud:
    """CHUNKS_DB Codex ITEM 2: an id returned by ``all_point_ids()`` whose row
    cannot be hydrated (``read`` -> ``None``) is a chunks.db primary-key/row
    INCONSISTENCY (corruption), not a normal state -- the enumerated-then-
    unhydratable id must RAISE ``ScrollDataIntegrityError`` naming the id, never
    be silently omitted (which drops a row AND can emit a terminal ``None``
    cursor falsely presenting a complete traversal, Messi #13).

    Constructed with a faithful fault injection: the REAL chunks.db and the REAL
    ``all_point_ids()`` are used unchanged (so the id IS enumerated); only
    ``read`` for that ONE id is stubbed to return ``None`` -- the exact
    primary-key-present-but-row-unhydratable inconsistency a normal SQLite table
    cannot produce on its own. The store under test (FilesystemVectorStore) is
    never mocked."""

    def test_read_none_for_enumerated_id_raises_naming_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        base_path = tmp_path / "index"
        base_path.mkdir()
        reader, collection_path = _build_temporal_chunks_db_collection(base_path)

        hole_id = TEMPORAL_IDS[0]
        real_factory = _scs_module.open_chunk_store_for_path

        def fake_factory(db_path, coll_path):  # type: ignore[no-untyped-def]
            store = real_factory(db_path, coll_path)
            orig_read = store.read

            def read_with_hole(pid):  # type: ignore[no-untyped-def]
                if pid == hole_id:
                    return None
                return orig_read(pid)

            store.read = read_with_hole  # type: ignore[method-assign]
            return store

        monkeypatch.setattr(_scs_module, "open_chunk_store_for_path", fake_factory)

        with pytest.raises(ScrollDataIntegrityError) as exc_info:
            reader.scroll_points(
                collection_name=TEMPORAL_COLLECTION,
                limit=100,
                with_payload=True,
            )
        assert hole_id in str(exc_info.value), (
            f"integrity error must name the unhydratable id: {exc_info.value!r}"
        )


class TestChunksDbWellFormedScrollPreserved:
    """Preservation: a well-formed CHUNKS_DB collection scrolls the full row set
    with correctly-dimensioned float vectors and paginates correctly -- the new
    validation and unhydratable-id gate add no false positives."""

    def test_well_formed_chunks_db_scrolls_with_vectors_true(
        self, tmp_path: Path
    ) -> None:
        base_path = tmp_path / "index"
        base_path.mkdir()
        reader, _ = _build_temporal_chunks_db_collection(base_path)

        points, next_cursor = reader.scroll_points(
            collection_name=TEMPORAL_COLLECTION,
            limit=100,
            with_payload=True,
            with_vectors=True,
        )
        assert {p["id"] for p in points} == ALL_TEMPORAL_IDS
        assert next_cursor is None
        for p in points:
            assert isinstance(p["vector"], list)
            assert len(p["vector"]) == VECTOR_SIZE
            assert all(isinstance(x, (int, float)) for x in p["vector"])

    def test_well_formed_chunks_db_paginates_completely(self, tmp_path: Path) -> None:
        base_path = tmp_path / "index"
        base_path.mkdir()
        reader, _ = _build_temporal_chunks_db_collection(base_path)

        seen: set = set()
        cursor = None
        pages = 0
        while True:
            pts, cursor = reader.scroll_points(
                collection_name=TEMPORAL_COLLECTION,
                limit=2,
                with_payload=True,
                with_vectors=True,
                offset=cursor,
            )
            seen |= {p["id"] for p in pts}
            pages += 1
            if cursor is None:
                break
            assert pages <= len(TEMPORAL_IDS) + 1, "pagination failed to terminate"
        assert seen == ALL_TEMPORAL_IDS


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
