"""Bug #1528: temporal collections must NEVER be written in the legacy
sharded ``vector_*.json`` layout for new indexing.

Epic #1454 exists to eliminate the one-JSON-file-per-chunk hash-sharded
layout, whose worst offender by orders of magnitude is temporal
(git-commit-history) indexing. As shipped, ``create_collection()``
hard-excluded every temporal collection from the CHUNKS_DB layout
unconditionally, so temporal indexing always produced the file explosion
(measured live: 487,076 ``vector_*.json`` files for one real repo).

Binding rule proven here:

  * a temporal collection created with an EXPLICIT
    ``use_chunks_db_for_new_collections=True`` is CHUNKS_DB (it was not,
    before this fix -- the temporal carve-out discarded the caller's
    explicit instruction, including the server's own
    ``--new-collection-layout=chunks_db`` child arg);
  * a temporal collection created with NO layout instruction at all
    (standalone CLI default) is ALSO CHUNKS_DB -- temporal never writes a
    new legacy JSON file, in any mode;
  * an EXPLICIT ``use_chunks_db_for_new_collections=False`` is still
    honored (SHARDED_JSON), so legacy-layout fixtures/back-compat callers
    remain buildable;
  * SEMANTIC collections keep their pre-existing default (SHARDED_JSON
    for CLI/daemon unless opted in) -- this fix must NOT re-flip the
    global default (Story #1488's context-dependent contract).

Real filesystem, real SQLite, real HNSW -- no mocking of the store's own
logic.
"""

import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Optional

import numpy as np
import pytest

from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore
from code_indexer.storage.shared.chunk_layout import (
    ChunkLayout,
    resolve_chunk_layout,
)

VECTOR_SIZE = 32
TEMPORAL_COLLECTION = "code-indexer-temporal-voyage_context_4-2024Q3"
SEMANTIC_COLLECTION = "code-indexer-voyage-code-3"

TEMPORAL_IDS = [f"proj:commit:{c * 8}:0" for c in "abcd"]


@pytest.fixture(autouse=True)
def _no_layout_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every test here reasons about the DEFAULT layout, so the process-wide
    opt-in env var must never leak in from the ambient environment."""
    monkeypatch.delenv("CIDX_CHUNKS_DB_NEW_COLLECTIONS", raising=False)


def _make_points(ids: List[str]) -> List[Dict[str, Any]]:
    rng = np.random.default_rng(1528)
    points: List[Dict[str, Any]] = []
    for i, pid in enumerate(ids):
        vector = rng.standard_normal(VECTOR_SIZE)
        vector[i % VECTOR_SIZE] += 25.0
        points.append(
            {
                "id": pid,
                "vector": vector.astype(np.float64).tolist(),
                "payload": {"path": f"file_{i}.py", "commit_hash": f"{i:040d}"},
            }
        )
    return points


def _index_points(
    store: FilesystemVectorStore, collection: str, ids: List[str]
) -> FilesystemVectorStore:
    store.create_collection(collection, vector_size=VECTOR_SIZE)
    store.begin_indexing(collection)
    store.upsert_points(collection, _make_points(ids))
    store.end_indexing(collection)
    return store


def _build_default(
    base_path: Path, collection: str, ids: List[str]
) -> FilesystemVectorStore:
    """Build with NO layout instruction at all -- the standalone-CLI default."""
    return _index_points(FilesystemVectorStore(base_path=base_path), collection, ids)


def _build_with_layout(
    base_path: Path, collection: str, ids: List[str], layout: Optional[bool]
) -> FilesystemVectorStore:
    """Build with an EXPLICIT layout instruction (True/False)."""
    return _index_points(
        FilesystemVectorStore(
            base_path=base_path, use_chunks_db_for_new_collections=layout
        ),
        collection,
        ids,
    )


def _json_chunk_files(collection_dir: Path) -> List[Path]:
    return sorted(collection_dir.rglob("vector_*.json"))


class TestTemporalDefaultsToChunksDb:
    def test_explicit_chunks_db_is_honored_for_temporal(self, tmp_path: Path) -> None:
        base_path = tmp_path / "index"
        _build_with_layout(base_path, TEMPORAL_COLLECTION, TEMPORAL_IDS, True)

        collection_dir = base_path / TEMPORAL_COLLECTION
        assert _json_chunk_files(collection_dir) == [], (
            "temporal collection built with an EXPLICIT chunks_db request "
            "still produced legacy vector_*.json files"
        )
        assert (collection_dir / "chunks.db").is_file()
        assert resolve_chunk_layout(collection_dir) == ChunkLayout.CHUNKS_DB

    def test_default_layout_is_chunks_db_for_temporal(self, tmp_path: Path) -> None:
        """No layout instruction at all (standalone CLI default) -- temporal
        must STILL be chunks_db: 'not a single json file for new indexing'."""
        base_path = tmp_path / "index"
        _build_default(base_path, TEMPORAL_COLLECTION, TEMPORAL_IDS)

        collection_dir = base_path / TEMPORAL_COLLECTION
        assert _json_chunk_files(collection_dir) == [], (
            "temporal collection built with the DEFAULT layout produced "
            "legacy vector_*.json files"
        )
        assert (collection_dir / "chunks.db").is_file()
        assert resolve_chunk_layout(collection_dir) == ChunkLayout.CHUNKS_DB

    def test_temporal_chunks_db_rows_are_readable(self, tmp_path: Path) -> None:
        """The consolidated temporal collection is not merely file-shaped
        correctly -- every written row is retrievable and searchable."""
        base_path = tmp_path / "index"
        store = _build_default(base_path, TEMPORAL_COLLECTION, TEMPORAL_IDS)

        for pid in TEMPORAL_IDS:
            point = store.get_point(pid, TEMPORAL_COLLECTION)
            assert point is not None, f"lost temporal row {pid}"
            assert point["id"] == pid

        query_vector = _make_points(TEMPORAL_IDS)[0]["vector"]
        results = store.search(
            query="",
            embedding_provider=None,
            collection_name=TEMPORAL_COLLECTION,
            limit=len(TEMPORAL_IDS),
            precomputed_query_vector=query_vector,
        )
        assert {r["id"] for r in results} == set(TEMPORAL_IDS)


class TestTemporalMetadataStillWritten:
    """The temporal METADATA store (``temporal_metadata`` rows) must be
    populated on the CHUNKS_DB write path exactly as on the legacy path.

    It is a separate store from the chunk data (a shared
    ``temporal_metadata.db`` in solo mode, PostgreSQL in cluster mode) and
    is what reconciliation (``reconcile_temporal_index``), the incremental
    gate and at-commit scoping all read. The batch write used to live ONLY
    inside the legacy sharded-JSON loop, so switching temporal to CHUNKS_DB
    silently stopped populating it.
    """

    def test_metadata_rows_are_persisted_for_chunks_db_temporal(
        self, tmp_path: Path
    ) -> None:
        base_path = tmp_path / "index"
        _build_default(base_path, TEMPORAL_COLLECTION, TEMPORAL_IDS)

        db_paths = sorted(base_path.rglob("temporal_metadata.db"))
        assert db_paths, (
            "no temporal_metadata.db was created by the CHUNKS_DB temporal "
            "write path -- reconciliation and incremental gating read it"
        )

        stored_point_ids: List[str] = []
        for db_path in db_paths:
            conn = sqlite3.connect(str(db_path))
            try:
                rows = conn.execute("SELECT point_id FROM temporal_metadata").fetchall()
            finally:
                conn.close()
            stored_point_ids.extend(row[0] for row in rows)

        # Exactly one row per indexed point, and they are the right points.
        assert len(stored_point_ids) == len(TEMPORAL_IDS), (
            f"expected exactly {len(TEMPORAL_IDS)} temporal metadata rows, "
            f"found {len(stored_point_ids)}"
        )
        assert set(stored_point_ids) == set(TEMPORAL_IDS), (
            "temporal metadata rows do not match the indexed points: "
            f"stored={sorted(stored_point_ids)} expected={sorted(TEMPORAL_IDS)}"
        )


class TestExplicitLegacyRequestsStillHonored:
    def test_explicit_sharded_json_still_honored_for_temporal(
        self, tmp_path: Path
    ) -> None:
        """An EXPLICIT sharded_json request is never silently upgraded --
        legacy fixtures and back-compat callers must remain buildable."""
        base_path = tmp_path / "index"
        _build_with_layout(base_path, TEMPORAL_COLLECTION, TEMPORAL_IDS, False)

        collection_dir = base_path / TEMPORAL_COLLECTION
        assert _json_chunk_files(collection_dir), (
            "an explicit sharded_json request must still produce legacy "
            "vector_*.json files"
        )
        assert not (collection_dir / "chunks.db").exists()
        assert resolve_chunk_layout(collection_dir) == ChunkLayout.SHARDED_JSON

    def test_semantic_default_stays_sharded_json(self, tmp_path: Path) -> None:
        """Story #1488's context-dependent contract: the CLI/daemon default
        for SEMANTIC collections stays SHARDED_JSON. This fix must not
        re-flip the global default."""
        base_path = tmp_path / "index"
        _build_default(base_path, SEMANTIC_COLLECTION, ["p0", "p1"])

        collection_dir = base_path / SEMANTIC_COLLECTION
        assert _json_chunk_files(collection_dir), (
            "semantic default layout changed -- expected SHARDED_JSON"
        )
        assert resolve_chunk_layout(collection_dir) == ChunkLayout.SHARDED_JSON
