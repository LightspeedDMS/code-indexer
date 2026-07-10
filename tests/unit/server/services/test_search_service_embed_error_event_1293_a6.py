"""Story #1293 S1b [A6]: search_service.py's Backend (non-FSV) branch must
emit an error event when the LIVE embedding call raises, before propagating
the exception -- covers the failover primary-attempt-fails scenario.
"""

from unittest.mock import MagicMock, patch

import pytest


def test_backend_path_emits_error_event_on_embedding_failure():
    from code_indexer.server.services.search_service import SemanticSearchService
    import code_indexer.server.services.search_service as ss_mod
    import code_indexer.server.services.governed_call as gc_mod

    mock_vsc = MagicMock()
    mock_vsc.resolve_collection_name.return_value = "test_coll"
    mock_backend = MagicMock()
    mock_backend.get_vector_store_client.return_value = mock_vsc

    with (
        patch.object(
            gc_mod,
            "coalesced_query_embedding",
            side_effect=RuntimeError("primary provider unreachable"),
        ),
        patch.object(
            ss_mod,
            "_load_repo_config",
            return_value={"embedding_provider": "voyage-ai"},
        ),
        patch("code_indexer.server.services.search_service.BackendFactory") as mock_bf,
        patch(
            "code_indexer.server.services.search_service.EmbeddingProviderFactory"
        ) as mock_epf,
        patch.object(ss_mod, "emit_embed_error_event") as mock_emit_error,
    ):
        mock_bf.create.return_value = mock_backend
        mock_provider = MagicMock()
        mock_provider.get_provider_name.return_value = "voyage-ai"
        mock_epf.create.return_value = mock_provider

        with pytest.raises(RuntimeError):
            SemanticSearchService()._perform_semantic_search(
                "/fake/repo", "q", 5, False
            )

    mock_emit_error.assert_called_once_with("voyage-ai")
