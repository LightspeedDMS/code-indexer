"""A failed clear of an existing multimodal collection aborts a full index."""

from unittest.mock import patch

import pytest

from code_indexer.config import VOYAGE_MULTIMODAL_MODEL
from code_indexer.services.smart_indexer import SmartIndexer
from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore


def test_existing_multimodal_collection_clear_failure_is_fatal(tmp_path):
    store = FilesystemVectorStore(tmp_path / "index", project_root=tmp_path)
    collection = store._get_collection_path(VOYAGE_MULTIMODAL_MODEL)
    collection.mkdir(parents=True)
    (collection / "collection_meta.json").write_text('{"vector_size": 2}')
    assert store.collection_exists(VOYAGE_MULTIMODAL_MODEL)

    indexer = SmartIndexer.__new__(SmartIndexer)
    indexer.vector_store_client = store
    with patch.object(store, "clear_collection", return_value=False) as clear:
        with pytest.raises(RuntimeError, match="multimodal collection"):
            indexer._clear_current_provider_multimodal_collection("voyage-ai")

    clear.assert_called_once_with(VOYAGE_MULTIMODAL_MODEL)
