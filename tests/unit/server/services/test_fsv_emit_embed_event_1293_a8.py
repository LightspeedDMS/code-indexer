"""Story #1293 S1b [A8]: FSV worker-thread emission via RETURN-META.

FilesystemVectorStore.search() runs the embedding call inside a
ThreadPoolExecutor worker (generate_embedding()); the returned
EmbeddingCacheMetadata is written to SearchEventContext in the MAIN THREAD via
_write_embed_meta_to_event_ctx() (Story #1159) because ContextVars do not
propagate into worker threads (Python 3.9). That existing consumption site is
also the correct chokepoint for the shared emit_embed_event() helper -- it
runs on the calling thread (correct correlation_id context), driven entirely
by the meta returned from the worker (never a guess).

These tests call _write_embed_meta_to_event_ctx directly (mirroring the
existing TestWriteEmbedMetaToEventCtxDirect pattern in
test_search_event_instrumentation.py) and assert emit_embed_event is invoked
with the exact meta object.
"""

from unittest.mock import patch


def test_write_embed_meta_to_event_ctx_calls_emit_embed_event():
    """RED->GREEN: _write_embed_meta_to_event_ctx must call emit_embed_event(meta)."""
    from code_indexer.server.services.governed_call import EmbeddingCacheMetadata
    from code_indexer.storage.filesystem_vector_store import (
        _write_embed_meta_to_event_ctx,
    )

    meta = EmbeddingCacheMetadata(
        key_found=False,
        cache_mode="on",
        provider_latency_ms=12,
        provider="voyage-ai",
        outcome="miss",
        role="direct",
    )

    with patch(
        "code_indexer.storage.filesystem_vector_store.emit_embed_event"
    ) as mock_emit:
        _write_embed_meta_to_event_ctx(meta, provider_name="voyage-ai")

    mock_emit.assert_called_once_with(meta)


def test_write_embed_meta_to_event_ctx_calls_emit_embed_event_for_cohere():
    """Same chokepoint serves both voyage and cohere providers."""
    from code_indexer.server.services.governed_call import EmbeddingCacheMetadata
    from code_indexer.storage.filesystem_vector_store import (
        _write_embed_meta_to_event_ctx,
    )

    meta = EmbeddingCacheMetadata(
        key_found=True,
        cache_mode="on",
        provider="cohere",
        outcome="hit",
        role="warm_hit",
    )

    with patch(
        "code_indexer.storage.filesystem_vector_store.emit_embed_event"
    ) as mock_emit:
        _write_embed_meta_to_event_ctx(meta, provider_name="cohere")

    mock_emit.assert_called_once_with(meta)


def test_fsv_search_emits_embed_event_end_to_end():
    """Real FSV.search() (parallel embed || index-load) drives emit_embed_event
    with the meta returned from the worker thread -- no mocking of FSV itself.
    """
    import numpy as np
    import tempfile
    from pathlib import Path
    from unittest.mock import MagicMock, patch as _patch

    from code_indexer.server.services.governed_call import EmbeddingCacheMetadata
    from code_indexer.storage.filesystem_vector_store import FilesystemVectorStore

    DIMS = 4
    FAKE_VEC = [0.1, 0.2, 0.3, 0.4]
    DIRECT_META = EmbeddingCacheMetadata(
        key_found=False,
        cache_mode="on",
        provider_latency_ms=5,
        provider="voyage-ai",
        outcome="miss",
        role="direct",
    )

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
            _patch.object(
                fsv_mod,
                "coalesced_query_embedding",
                return_value=(FAKE_VEC, DIRECT_META),
            ),
            _patch.object(fsv_mod, "emit_embed_event") as mock_emit,
        ):
            store.search(
                query="test query",
                embedding_provider=mock_provider,
                collection_name="test_coll",
                limit=5,
            )

    mock_emit.assert_called_once_with(DIRECT_META)
