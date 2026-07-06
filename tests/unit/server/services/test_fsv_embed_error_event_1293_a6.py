"""Story #1293 S1b [A6]: FSV worker-thread embedding failure must also emit
an error event before the exception propagates to the caller (the FSV path
is the PRIMARY production storage backend, so failover through it must be
covered the same way as the Backend/non-FSV path).
"""

from unittest.mock import MagicMock, patch


def test_fsv_search_emits_error_event_on_embedding_failure():
    import numpy as np
    import tempfile
    from pathlib import Path

    from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

    DIMS = 4
    FAKE_VEC = [0.1, 0.2, 0.3, 0.4]

    with tempfile.TemporaryDirectory() as tmp_dir:
        base_path = Path(tmp_dir)
        store = FilesystemVectorStore(base_path=base_path, project_root=base_path)
        store.create_collection("test_coll", vector_size=DIMS)
        vec = np.array(FAKE_VEC, dtype=np.float32)
        store.upsert_points(
            "test_coll",
            [
                {
                    "id": "pt1",
                    "vector": vec.tolist(),
                    "payload": {"content": "hello", "file_path": "a.py"},
                }
            ],
        )
        store.end_indexing("test_coll")

        mock_provider = MagicMock()
        mock_provider.get_provider_name.return_value = "voyage-ai"

        import code_indexer.storage.filesystem_vector_store as fsv_mod

        with (
            patch.object(
                fsv_mod,
                "coalesced_query_embedding",
                side_effect=RuntimeError("primary provider unreachable"),
            ),
            patch.object(fsv_mod, "emit_embed_error_event") as mock_emit_error,
        ):
            raised = None
            try:
                store.search(
                    query="test query",
                    embedding_provider=mock_provider,
                    collection_name="test_coll",
                    limit=5,
                )
            except RuntimeError as exc:
                raised = exc

    assert raised is not None, "expected the embedding failure to propagate"
    mock_emit_error.assert_called_once_with("voyage-ai")
