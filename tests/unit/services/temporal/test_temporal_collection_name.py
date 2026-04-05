"""Test for temporal collection name backward compatibility.

Story #628: TEMPORAL_COLLECTION_NAME class constants were removed and replaced
with provider-aware naming. These tests verify backward compatibility:
- TemporalIndexer still uses 'code-indexer-temporal' as the default collection name
- TemporalSearchService accepts collection_name and stores it as instance attribute
"""

from pathlib import Path
from unittest.mock import Mock, patch


def _make_temporal_indexer_default(tmp_path: Path):
    """Construct TemporalIndexer with minimal mocks and no explicit collection_name."""
    from code_indexer.services.temporal.temporal_indexer import TemporalIndexer

    mock_config = Mock()
    mock_config.voyage_ai = Mock()
    mock_config.voyage_ai.max_concurrent_batches_per_commit = 10
    mock_config.embedding_provider = "voyage-ai"
    mock_config.voyage_ai.model = "voyage-code-3"
    mock_config.temporal = Mock()
    mock_config.temporal.diff_context_lines = 3
    mock_config.file_extensions = []
    mock_config.override_config = None

    mock_config_manager = Mock()
    mock_config_manager.get_config.return_value = mock_config

    mock_vector_store = Mock()
    mock_vector_store.project_root = tmp_path
    mock_vector_store.base_path = tmp_path / ".code-indexer" / "index"
    mock_vector_store.collection_exists.return_value = True
    mock_vector_store.load_id_index.return_value = set()

    with patch(
        "code_indexer.services.embedding_factory.EmbeddingProviderFactory.get_provider_model_info",
        return_value={
            "provider": "voyage-ai",
            "model": "voyage-code-3",
            "dimensions": 1024,
        },
    ):
        return TemporalIndexer(
            config_manager=mock_config_manager,
            vector_store=mock_vector_store,
            # No collection_name supplied — tests the default
        )


def test_temporal_indexer_default_collection_name(tmp_path):
    """TemporalIndexer defaults to 'code-indexer-temporal' without explicit collection_name.

    Story #628: TEMPORAL_COLLECTION_NAME class constant was removed.
    Backward compat is maintained via a default parameter value that sets
    instance.collection_name to the legacy value.
    """
    indexer = _make_temporal_indexer_default(tmp_path)
    assert indexer.collection_name == "code-indexer-temporal", (
        f"Default must be 'code-indexer-temporal' for backward compat, got: {indexer.collection_name!r}"
    )


def test_temporal_search_service_stores_collection_name(tmp_path):
    """TemporalSearchService stores the supplied collection_name as instance attribute.

    Story #628: TEMPORAL_COLLECTION_NAME class constant was removed.
    Callers resolve the collection name via resolve_temporal_collection_from_config()
    and pass it in. The service stores whatever is supplied.
    """
    from code_indexer.services.temporal.temporal_search_service import (
        TemporalSearchService,
    )

    service = TemporalSearchService(
        config_manager=Mock(),
        project_root=tmp_path,
        collection_name="code-indexer-temporal",
    )
    assert service.collection_name == "code-indexer-temporal", (
        f"TemporalSearchService must store supplied collection_name, got: {service.collection_name!r}"
    )
