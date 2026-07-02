"""Tests for temporal_collection_naming module.

TDD: These tests are written BEFORE the implementation to drive the design.

Covers:
- resolve_temporal_collection_name: sanitizes model name, builds provider-aware name
- is_temporal_collection: recognizes legacy AND provider-aware collection names
- get_model_name_for_provider: reads model from config for a given provider
- resolve_temporal_collection_from_config: convenience combining both lookups
- get_temporal_collections: enumerate on-disk temporal collections
- Module constants: LEGACY_TEMPORAL_COLLECTION, TEMPORAL_COLLECTION_PREFIX
- TemporalIndexer collection_name parameter validation
"""

import pytest
from pathlib import Path
from unittest.mock import MagicMock, Mock, patch


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def voyage_config():
    """Config mock configured for voyage-ai provider."""
    config = MagicMock()
    config.embedding_provider = "voyage-ai"
    config.voyage_ai.model = "voyage-code-3"
    return config


def _make_temporal_indexer_mocks(tmp_path: Path):
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

    mock_vector_store = Mock()
    mock_vector_store.project_root = tmp_path
    mock_vector_store.base_path = tmp_path / ".code-indexer" / "index"
    mock_vector_store.collection_exists.return_value = True
    mock_vector_store.load_id_index.return_value = set()

    return mock_config_manager, mock_vector_store


# ---------------------------------------------------------------------------
# resolve_temporal_collection_name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "model_name, expected",
    [
        ("voyage-code-3", "code-indexer-temporal-voyage_code_3"),
        ("embed-v4.0", "code-indexer-temporal-embed_v4_0"),
        ("My Model/v2.1+", "code-indexer-temporal-my_model_v2_1_"),
        ("VOYAGE-CODE-3", "code-indexer-temporal-voyage_code_3"),
    ],
)
def test_resolve_temporal_collection_name(model_name, expected):
    """Sanitizes model name and builds provider-aware collection name."""
    from code_indexer.services.temporal.temporal_collection_naming import (
        resolve_temporal_collection_name,
    )

    assert resolve_temporal_collection_name(model_name) == expected


def test_resolve_temporal_collection_name_has_prefix():
    """All results start with the standard prefix."""
    from code_indexer.services.temporal.temporal_collection_naming import (
        TEMPORAL_COLLECTION_PREFIX,
        resolve_temporal_collection_name,
    )

    result = resolve_temporal_collection_name("any-model")
    assert result.startswith(TEMPORAL_COLLECTION_PREFIX)


# ---------------------------------------------------------------------------
# is_temporal_collection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "collection_name, expected",
    [
        ("code-indexer-temporal", True),
        ("code-indexer-temporal-voyage_code_3", True),
        ("code-indexer-temporal-embed_v4_0", True),
        ("voyage-code-3", False),
        ("code-indexer", False),
        ("", False),
        ("code-indexer-temp", False),
    ],
)
def test_is_temporal_collection(collection_name, expected):
    """Recognizes legacy and provider-aware temporal collections."""
    from code_indexer.services.temporal.temporal_collection_naming import (
        is_temporal_collection,
    )

    assert is_temporal_collection(collection_name) is expected


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------


def test_legacy_temporal_collection_constant():
    """LEGACY_TEMPORAL_COLLECTION equals 'code-indexer-temporal'."""
    from code_indexer.services.temporal.temporal_collection_naming import (
        LEGACY_TEMPORAL_COLLECTION,
    )

    assert LEGACY_TEMPORAL_COLLECTION == "code-indexer-temporal"


def test_temporal_collection_prefix_constant():
    """TEMPORAL_COLLECTION_PREFIX equals 'code-indexer-temporal-'."""
    from code_indexer.services.temporal.temporal_collection_naming import (
        TEMPORAL_COLLECTION_PREFIX,
    )

    assert TEMPORAL_COLLECTION_PREFIX == "code-indexer-temporal-"


# ---------------------------------------------------------------------------
# get_model_name_for_provider
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "provider, model_attr, model_value",
    [
        ("voyage-ai", "voyage_ai", "voyage-code-3"),
        ("cohere", "cohere", "embed-v4.0"),
    ],
)
def test_get_model_name_for_provider(provider, model_attr, model_value):
    """Returns correct model name for each supported provider."""
    from code_indexer.services.temporal.temporal_collection_naming import (
        get_model_name_for_provider,
    )

    config = MagicMock()
    config.embedding_provider = provider
    getattr(config, model_attr).model = model_value

    result = get_model_name_for_provider(provider, config)
    assert result == model_value


def test_get_model_name_for_provider_unknown():
    """Raises ValueError for unknown provider."""
    from code_indexer.services.temporal.temporal_collection_naming import (
        get_model_name_for_provider,
    )

    config = MagicMock()
    with pytest.raises(ValueError, match="Unknown provider"):
        get_model_name_for_provider("unknown-provider", config)


# ---------------------------------------------------------------------------
# resolve_temporal_collection_from_config
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "provider, model_attr, model_value, expected",
    [
        (
            "voyage-ai",
            "voyage_ai",
            "voyage-code-3",
            "code-indexer-temporal-voyage_code_3",
        ),
        ("cohere", "cohere", "embed-v4.0", "code-indexer-temporal-embed_v4_0"),
    ],
)
def test_resolve_temporal_collection_from_config(
    provider, model_attr, model_value, expected
):
    """Reads provider config and produces provider-aware collection name."""
    from code_indexer.services.temporal.temporal_collection_naming import (
        resolve_temporal_collection_from_config,
    )

    config = MagicMock()
    config.embedding_provider = provider
    getattr(config, model_attr).model = model_value

    result = resolve_temporal_collection_from_config(config)
    assert result == expected


# ---------------------------------------------------------------------------
# get_temporal_collections (disk enumeration)
# ---------------------------------------------------------------------------


def test_get_temporal_collections_finds_legacy(tmp_path, voyage_config):
    """Finds legacy 'code-indexer-temporal' directory on disk."""
    from code_indexer.services.temporal.temporal_collection_naming import (
        get_temporal_collections,
    )

    index_path = tmp_path / "index"
    (index_path / "code-indexer-temporal").mkdir(parents=True)
    (index_path / "voyage-code-3").mkdir(parents=True)

    results = get_temporal_collections(voyage_config, index_path)
    names = [name for name, _ in results]
    assert "code-indexer-temporal" in names
    assert "voyage-code-3" not in names


def test_get_temporal_collections_finds_provider_aware(tmp_path, voyage_config):
    """Finds provider-aware 'code-indexer-temporal-voyage_code_3' directory."""
    from code_indexer.services.temporal.temporal_collection_naming import (
        get_temporal_collections,
    )

    index_path = tmp_path / "index"
    (index_path / "code-indexer-temporal-voyage_code_3").mkdir(parents=True)
    (index_path / "some-other-dir").mkdir(parents=True)

    results = get_temporal_collections(voyage_config, index_path)
    names = [name for name, _ in results]
    assert "code-indexer-temporal-voyage_code_3" in names
    assert "some-other-dir" not in names


def test_get_temporal_collections_returns_paths(tmp_path, voyage_config):
    """Returns (name, path) tuples with correct paths."""
    from code_indexer.services.temporal.temporal_collection_naming import (
        get_temporal_collections,
    )

    index_path = tmp_path / "index"
    temporal_dir = index_path / "code-indexer-temporal"
    temporal_dir.mkdir(parents=True)

    results = get_temporal_collections(voyage_config, index_path)
    assert len(results) == 1
    name, path = results[0]
    assert name == "code-indexer-temporal"
    assert path == temporal_dir


def test_get_temporal_collections_empty_index(tmp_path, voyage_config):
    """Returns empty list when no temporal collections exist."""
    from code_indexer.services.temporal.temporal_collection_naming import (
        get_temporal_collections,
    )

    index_path = tmp_path / "index"
    index_path.mkdir(parents=True)
    (index_path / "voyage-code-3").mkdir()

    results = get_temporal_collections(voyage_config, index_path)
    assert results == []


def test_get_temporal_collections_nonexistent_index(tmp_path, voyage_config):
    """Returns empty list when index directory doesn't exist."""
    from code_indexer.services.temporal.temporal_collection_naming import (
        get_temporal_collections,
    )

    index_path = tmp_path / "nonexistent"
    results = get_temporal_collections(voyage_config, index_path)
    assert results == []


# ---------------------------------------------------------------------------
# TemporalIndexer collection_name parameter validation
# ---------------------------------------------------------------------------


def _make_indexer(tmp_path, collection_name):
    """Helper: construct TemporalIndexer with given collection_name."""
    from code_indexer.services.temporal.temporal_indexer import TemporalIndexer

    mock_config_manager, mock_vector_store = _make_temporal_indexer_mocks(tmp_path)
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


def test_temporal_indexer_accepts_valid_collection_name(tmp_path):
    """TemporalIndexer accepts a valid collection name and stores it."""
    indexer = _make_indexer(tmp_path, "code-indexer-temporal-voyage_code_3")
    assert indexer.collection_name == "code-indexer-temporal-voyage_code_3"


def test_temporal_indexer_rejects_empty_collection_name(tmp_path):
    """TemporalIndexer raises ValueError for empty collection_name."""
    with pytest.raises(ValueError):
        _make_indexer(tmp_path, "")


@pytest.mark.parametrize(
    "bad_name",
    ["../escape", "a/../b", "subdir/child", ".", ".."],
)
def test_temporal_indexer_rejects_path_traversal(tmp_path, bad_name):
    """TemporalIndexer raises ValueError for path traversal in collection_name."""
    with pytest.raises(ValueError):
        _make_indexer(tmp_path, bad_name)


def test_temporal_indexer_rejects_absolute_path(tmp_path):
    """TemporalIndexer raises ValueError for absolute path as collection_name."""
    with pytest.raises(ValueError):
        _make_indexer(tmp_path, "/abs/path")
