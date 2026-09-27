"""A cached vector count cannot prove that a clear rebuilt TEXT rows."""

import json
import sqlite3
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from code_indexer.services.provider_rebuild_check import (
    find_providers_not_rebuilt_since,
)
from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore


@pytest.mark.parametrize("layout", ["sharded_json", "chunks_db"])
@pytest.mark.parametrize("has_rows", [False, True])
def test_rebuild_check_uses_physical_rows_despite_stale_cached_count(
    tmp_path: Path, layout: str, has_rows: bool
) -> None:
    config_dir = tmp_path / ".code-indexer"
    config_dir.mkdir()
    (config_dir / "metadata-voyage-ai.json").write_text(
        json.dumps({"status": "completed", "last_index_timestamp": 200.0})
    )

    index_dir = config_dir / "index"
    collection = index_dir / "example-text"
    collection.mkdir(parents=True)
    collection_meta = {"hnsw_index": {"vector_count": 7}}
    if layout == "chunks_db":
        collection_meta["chunks_db"] = {"version": 1}
        with sqlite3.connect(collection / "chunks.db") as conn:
            conn.execute("CREATE TABLE chunks (id TEXT PRIMARY KEY)")
            if has_rows:
                conn.execute("INSERT INTO chunks (id) VALUES ('example-chunk')")
    elif has_rows:
        (collection / "vector_example.json").write_text('{"id": "example-chunk"}')
    (collection / "collection_meta.json").write_text(json.dumps(collection_meta))

    store = SimpleNamespace(base_path=index_dir)
    store.resolve_collection_name = lambda config, provider: "example-text"
    store._get_collection_path = lambda name: index_dir / name
    store.count_points = lambda name: FilesystemVectorStore.count_points(store, name)
    assert store.count_points("example-text") == 7

    with patch(
        "code_indexer.services.embedding_factory.EmbeddingProviderFactory.create"
    ):
        missing = find_providers_not_rebuilt_since(
            config_dir,
            ["voyage-ai"],
            100.0,
            config=SimpleNamespace(),
            vector_store=store,
        )

    assert missing == ([] if has_rows else ["voyage-ai"])
