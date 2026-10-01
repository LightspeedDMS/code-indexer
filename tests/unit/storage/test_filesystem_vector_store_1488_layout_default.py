"""Story #1488: new collections default to the legacy SHARDED_JSON layout
for the CLI/daemon path -- the server states the layout explicitly via
``--new-collection-layout=chunks_db`` instead of relying on a flipped
global default.

Story #1456 introduced ``use_chunks_db_for_new_collections`` as an opt-in
gate defaulting to False (env var ``CIDX_CHUNKS_DB_NEW_COLLECTIONS`` unset
-> False). Bug #1486 Fix B briefly flipped that effective default to True;
Story #1488 SUPERSEDES Fix B and restores the original semantics: unset
env -> SHARDED_JSON, an explicit truthy env value ("1"/"true"/"yes",
case-insensitive) -> CHUNKS_DB, anything else -> SHARDED_JSON, and an
explicit constructor param ALWAYS wins over the env var.
"""

from unittest.mock import Mock

import numpy as np
import pytest

from code_indexer.storage.filesystem_vector_store import (
    FilesystemVectorStore,
    _parse_use_chunks_db_for_new_collections_env,
)
from code_indexer.storage.shared.chunk_layout import ChunkLayout, resolve_chunk_layout

VECTOR_DIM = 16

#: Sentinel meaning "do not pass the constructor param at all" -- distinct
#: from passing an explicit None (both currently mean "fall back to env",
#: but the sentinel keeps the test matrix honest about the default path).
_UNSET = object()


def _points(vectors) -> list:
    return [
        {
            "id": f"vec_{i}",
            "vector": v.astype(np.float32).tolist(),
            "payload": {"path": f"vec_{i}.py", "language": "python"},
        }
        for i, v in enumerate(vectors)
    ]


def _make_store(tmp_path, monkeypatch, env, ctor) -> FilesystemVectorStore:
    if env is None:
        monkeypatch.delenv("CIDX_CHUNKS_DB_NEW_COLLECTIONS", raising=False)
    else:
        monkeypatch.setenv("CIDX_CHUNKS_DB_NEW_COLLECTIONS", env)
    kwargs = {} if ctor is _UNSET else {"use_chunks_db_for_new_collections": ctor}
    return FilesystemVectorStore(base_path=tmp_path, **kwargs)


def _index_collection(store, count, seed):
    store.create_collection("coll", vector_size=VECTOR_DIM)
    collection_path = store._get_collection_path("coll")
    store.begin_indexing("coll")
    rng = np.random.default_rng(seed)
    vectors = [rng.standard_normal(VECTOR_DIM) for _ in range(count)]
    store.upsert_points("coll", _points(vectors))
    result = store.end_indexing("coll")
    return collection_path, vectors, result


class TestEnvParseContract:
    def test_env_var_unset_defaults_to_false(self, monkeypatch) -> None:
        monkeypatch.delenv("CIDX_CHUNKS_DB_NEW_COLLECTIONS", raising=False)

        assert _parse_use_chunks_db_for_new_collections_env() is False

    @pytest.mark.parametrize("truthy", ["1", "true", "yes", "TRUE", "Yes"])
    def test_env_var_truthy_opts_in(self, monkeypatch, truthy: str) -> None:
        monkeypatch.setenv("CIDX_CHUNKS_DB_NEW_COLLECTIONS", truthy)

        assert _parse_use_chunks_db_for_new_collections_env() is True

    @pytest.mark.parametrize(
        "falsy", ["0", "false", "no", "off", "garbage", "", "2", "enabled"]
    )
    def test_env_var_anything_else_is_false(self, monkeypatch, falsy: str) -> None:
        monkeypatch.setenv("CIDX_CHUNKS_DB_NEW_COLLECTIONS", falsy)

        assert _parse_use_chunks_db_for_new_collections_env() is False


class TestLayoutPrecedenceEndToEnd:
    """Full create -> begin -> upsert -> end lifecycle proving the layout
    precedence matrix genuinely lands on disk (chunks.db vs vector_*.json),
    not just as an in-memory flag."""

    @pytest.mark.parametrize(
        "env, ctor, expected",
        [
            (None, _UNSET, ChunkLayout.SHARDED_JSON),  # default
            ("false", _UNSET, ChunkLayout.SHARDED_JSON),  # env opt-out
            ("garbage", _UNSET, ChunkLayout.SHARDED_JSON),  # env junk
            ("true", _UNSET, ChunkLayout.CHUNKS_DB),  # env opt-in
            (None, True, ChunkLayout.CHUNKS_DB),  # ctor True beats unset
            ("true", False, ChunkLayout.SHARDED_JSON),  # ctor False beats env
            ("true", True, ChunkLayout.CHUNKS_DB),  # ctor True + truthy env
            ("false", True, ChunkLayout.CHUNKS_DB),  # ctor True beats falsy env
        ],
    )
    def test_new_collection_layout_precedence(
        self, tmp_path, monkeypatch, env, ctor, expected
    ) -> None:
        store = _make_store(tmp_path, monkeypatch, env, ctor)
        collection_path, _vectors, result = _index_collection(store, count=6, seed=5)

        assert result["vectors_indexed"] == 6
        assert resolve_chunk_layout(collection_path) == expected
        if expected == ChunkLayout.CHUNKS_DB:
            assert (collection_path / "chunks.db").exists()
            assert list(collection_path.rglob("vector_*.json")) == []
        else:
            assert not (collection_path / "chunks.db").exists()
            assert len(list(collection_path.rglob("vector_*.json"))) == 6

    def test_default_constructed_store_records_no_chunks_db_intent(
        self, tmp_path, monkeypatch
    ) -> None:
        monkeypatch.delenv("CIDX_CHUNKS_DB_NEW_COLLECTIONS", raising=False)

        store = FilesystemVectorStore(base_path=tmp_path)
        store.create_collection("coll", vector_size=VECTOR_DIM)

        assert store._chunks_db_mode.get("coll") is not True

    def test_default_constructed_store_is_queryable(
        self, tmp_path, monkeypatch
    ) -> None:
        store = _make_store(tmp_path, monkeypatch, env=None, ctor=_UNSET)
        _collection_path, vectors, _result = _index_collection(store, count=10, seed=9)

        results = store.search(
            query="unused",
            embedding_provider=Mock(),
            collection_name="coll",
            limit=5,
            precomputed_query_vector=vectors[0].tolist(),
        )

        assert len(results) > 0
        assert results[0]["id"] == "vec_0"
