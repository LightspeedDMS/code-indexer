"""Tests for dual temporal indexing — Story #631.

Covers new dual-provider logic in TemporalIndexer:
- _get_all_provider_configs() returns configured providers
- indexed_blobs changed from set to dict[str, set]
- _get_progress() returns per-collection TemporalProgressiveMetadata
- Single-provider behavior unchanged (backward compat)
"""

from pathlib import Path
from unittest.mock import Mock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_base_mocks(tmp_path: Path):
    """Create mock config_manager and vector_store for TemporalIndexer construction."""
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
    mock_config_manager.config_path = tmp_path / "config.json"

    mock_vector_store = Mock()
    mock_vector_store.project_root = tmp_path
    mock_vector_store.base_path = tmp_path / ".code-indexer" / "index"
    mock_vector_store.collection_exists.return_value = True
    mock_vector_store.load_id_index.return_value = set()

    return mock_config_manager, mock_vector_store, mock_config


def _make_indexer(
    tmp_path: Path, collection_name: str = "code-indexer-temporal-voyage_code_3"
):
    """Construct TemporalIndexer with patched embedding factory."""
    from code_indexer.services.temporal.temporal_indexer import TemporalIndexer

    mock_config_manager, mock_vector_store, _ = _make_base_mocks(tmp_path)
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
            collection_name=collection_name,
        )


# ---------------------------------------------------------------------------
# Test: indexed_blobs is dict[str, set] not a bare set
# ---------------------------------------------------------------------------


def test_indexed_blobs_is_dict(tmp_path):
    """indexed_blobs must be a dict keyed by collection_name, not a bare set."""
    indexer = _make_indexer(tmp_path)
    assert isinstance(indexer.indexed_blobs, dict), (
        "indexed_blobs must be dict[str, set], got "
        + type(indexer.indexed_blobs).__name__
    )


def test_indexed_blobs_starts_empty(tmp_path):
    """indexed_blobs starts as an empty dict on construction."""
    indexer = _make_indexer(tmp_path)
    assert indexer.indexed_blobs == {}


def test_indexed_blobs_per_collection_independent(tmp_path):
    """Blob sets for different collections do not share state."""
    indexer = _make_indexer(tmp_path)

    coll_a = "code-indexer-temporal-voyage_code_3"
    coll_b = "code-indexer-temporal-embed_v4_0"

    blobs_a = indexer.indexed_blobs.setdefault(coll_a, set())
    blobs_a.add("blob-hash-1")

    blobs_b = indexer.indexed_blobs.setdefault(coll_b, set())
    blobs_b.add("blob-hash-2")

    assert "blob-hash-1" in indexer.indexed_blobs[coll_a]
    assert "blob-hash-2" not in indexer.indexed_blobs[coll_a]
    assert "blob-hash-2" in indexer.indexed_blobs[coll_b]
    assert "blob-hash-1" not in indexer.indexed_blobs[coll_b]


# ---------------------------------------------------------------------------
# Test: _get_progress() returns per-collection TemporalProgressiveMetadata
# ---------------------------------------------------------------------------


def test_get_progress_returns_temporal_progressive_metadata(tmp_path):
    """_get_progress(collection_name) returns a TemporalProgressiveMetadata instance."""
    from code_indexer.services.temporal.temporal_progressive_metadata import (
        TemporalProgressiveMetadata,
    )

    indexer = _make_indexer(tmp_path)
    prog = indexer._get_progress("code-indexer-temporal-voyage_code_3")
    assert isinstance(prog, TemporalProgressiveMetadata)


def test_get_progress_creates_directory(tmp_path):
    """_get_progress() creates the collection directory if it does not exist."""
    indexer = _make_indexer(tmp_path)
    coll_name = "code-indexer-temporal-embed_v4_0"

    # Directory should not exist yet
    coll_dir = indexer.vector_store.base_path / coll_name
    assert not coll_dir.exists()

    indexer._get_progress(coll_name)

    assert coll_dir.exists()


def test_get_progress_same_instance_on_repeated_calls(tmp_path):
    """_get_progress() returns the same instance when called twice for the same collection."""
    indexer = _make_indexer(tmp_path)
    coll_name = "code-indexer-temporal-voyage_code_3"

    prog_first = indexer._get_progress(coll_name)
    prog_second = indexer._get_progress(coll_name)

    assert prog_first is prog_second


def test_get_progress_separate_instances_for_different_collections(tmp_path):
    """_get_progress() returns separate instances for different collections."""
    indexer = _make_indexer(tmp_path)
    coll_a = "code-indexer-temporal-voyage_code_3"
    coll_b = "code-indexer-temporal-embed_v4_0"

    prog_a = indexer._get_progress(coll_a)
    prog_b = indexer._get_progress(coll_b)

    assert prog_a is not prog_b


# ---------------------------------------------------------------------------
# Test: _get_all_provider_configs() returns configured providers
# ---------------------------------------------------------------------------


def test_get_all_provider_configs_returns_list(tmp_path):
    """_get_all_provider_configs() returns a list."""
    indexer = _make_indexer(tmp_path)
    with (
        patch(
            "code_indexer.services.embedding_factory.EmbeddingProviderFactory.get_configured_providers",
            return_value=["voyage-ai"],
        ),
        patch(
            "code_indexer.services.embedding_factory.EmbeddingProviderFactory.create",
            return_value=Mock(),
        ),
    ):
        result = indexer._get_all_provider_configs()
    assert isinstance(result, list)


def test_get_all_provider_configs_single_provider(tmp_path):
    """With one provider configured, returns exactly one entry."""
    indexer = _make_indexer(tmp_path)
    mock_provider = Mock()

    with (
        patch(
            "code_indexer.services.embedding_factory.EmbeddingProviderFactory.get_configured_providers",
            return_value=["voyage-ai"],
        ),
        patch(
            "code_indexer.services.embedding_factory.EmbeddingProviderFactory.create",
            return_value=mock_provider,
        ),
    ):
        result = indexer._get_all_provider_configs()

    assert len(result) == 1
    coll_name, provider, model_name = result[0]
    assert coll_name == "code-indexer-temporal-voyage_code_3"
    assert provider is mock_provider
    assert model_name == "voyage-code-3"


def test_get_all_provider_configs_two_providers(tmp_path):
    """With two providers configured, returns two entries."""
    mock_config_manager, mock_vector_store, mock_config = _make_base_mocks(tmp_path)

    # Configure cohere in mock config
    mock_config.cohere = Mock()
    mock_config.cohere.model = "embed-v4.0"

    from code_indexer.services.temporal.temporal_indexer import TemporalIndexer

    with patch(
        "code_indexer.services.embedding_factory.EmbeddingProviderFactory.get_provider_model_info",
        return_value={
            "provider": "voyage-ai",
            "model": "voyage-code-3",
            "dimensions": 1024,
        },
    ):
        indexer = TemporalIndexer(
            config_manager=mock_config_manager,
            vector_store=mock_vector_store,
            collection_name="code-indexer-temporal-voyage_code_3",
        )

    voyage_provider = Mock()
    cohere_provider = Mock()

    def _fake_create(config, provider_name=None):
        if provider_name == "voyage-ai":
            return voyage_provider
        return cohere_provider

    with (
        patch(
            "code_indexer.services.embedding_factory.EmbeddingProviderFactory.get_configured_providers",
            return_value=["voyage-ai", "cohere"],
        ),
        patch(
            "code_indexer.services.embedding_factory.EmbeddingProviderFactory.create",
            side_effect=_fake_create,
        ),
    ):
        result = indexer._get_all_provider_configs()

    assert len(result) == 2
    collection_names = [entry[0] for entry in result]
    assert "code-indexer-temporal-voyage_code_3" in collection_names
    assert "code-indexer-temporal-embed_v4_0" in collection_names


def test_get_all_provider_configs_fallback_when_no_providers(tmp_path):
    """When no providers configured, falls back to primary collection."""
    indexer = _make_indexer(tmp_path)

    with patch(
        "code_indexer.services.embedding_factory.EmbeddingProviderFactory.get_configured_providers",
        return_value=[],
    ):
        result = indexer._get_all_provider_configs()

    assert len(result) == 1
    coll_name, provider, model_name = result[0]
    assert coll_name == indexer.collection_name


def test_get_all_provider_configs_skips_failed_provider(tmp_path):
    """Provider that raises on create() is skipped with a warning."""
    indexer = _make_indexer(tmp_path)

    def _fake_create(config, provider_name=None):
        if provider_name == "cohere":
            raise RuntimeError("Cohere unavailable")
        return Mock()

    with (
        patch(
            "code_indexer.services.embedding_factory.EmbeddingProviderFactory.get_configured_providers",
            return_value=["voyage-ai", "cohere"],
        ),
        patch(
            "code_indexer.services.embedding_factory.EmbeddingProviderFactory.create",
            side_effect=_fake_create,
        ),
    ):
        result = indexer._get_all_provider_configs()

    # Only voyage-ai succeeds; cohere is skipped
    assert len(result) == 1
    coll_name, _, _ = result[0]
    assert coll_name == "code-indexer-temporal-voyage_code_3"


# ---------------------------------------------------------------------------
# Test: single-provider backward compatibility
# ---------------------------------------------------------------------------


def test_single_provider_collection_name_unchanged(tmp_path):
    """With single provider, collection_name matches primary collection (backward compat)."""
    indexer = _make_indexer(tmp_path, "code-indexer-temporal-voyage_code_3")
    assert indexer.collection_name == "code-indexer-temporal-voyage_code_3"


def test_progressive_metadata_still_accessible(tmp_path):
    """progressive_metadata attribute still accessible for backward compat."""
    indexer = _make_indexer(tmp_path)
    from code_indexer.services.temporal.temporal_progressive_metadata import (
        TemporalProgressiveMetadata,
    )

    assert isinstance(indexer.progressive_metadata, TemporalProgressiveMetadata)
