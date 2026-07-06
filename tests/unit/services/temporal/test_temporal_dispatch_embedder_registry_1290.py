"""Story #1290: temporal query discovery/provider-construction must key off
config.temporal.embedders (the per-commit embedder adapter registry), NOT the
REGULAR semantic-search config.voyage_ai.model / config.cohere.model.

Before this fix, `_discover_provider_shards_with_pruning` and
`_create_embedding_provider_for_collection` resolved provider/model names via
`EmbeddingProviderFactory.get_configured_providers` + `get_model_name_for_provider`,
which reads `config.voyage_ai.model` (e.g. "voyage-code-3", the REGULAR
semantic-search model) -- but the per-commit temporal indexer places shards
under `config.temporal.active_embedder` (e.g. "voyage-context-4"). Any real
deployment with voyage_ai.model != temporal.active_embedder would silently
find ZERO shards on every temporal query.
"""

from datetime import datetime, timezone

from code_indexer.config import VoyageAIConfig
from code_indexer.services.temporal.temporal_collection_naming import (
    get_shard_collection_name,
)
from code_indexer.services.temporal.temporal_fusion_dispatch import (
    _create_embedding_provider_for_collection,
    _discover_provider_shards_with_pruning,
)
from code_indexer.services.temporal.temporal_structure_marker import (
    write_structure_marker,
)


class _FakeTemporalConfig:
    embedders = ["voyage-context-4"]
    active_embedder = "voyage-context-4"
    aggregation_chunk_chars = 4096
    diff_context_lines = 5


class _FakeConfig:
    """Minimal stand-in exposing exactly the attrs the dispatch layer reads."""

    def __init__(self) -> None:
        # Regular semantic-search model is DELIBERATELY different from the
        # temporal active_embedder to prove discovery doesn't cross-wire them.
        self.voyage_ai = VoyageAIConfig(model="voyage-code-3")
        self.embedding_provider = "voyage-ai"
        self.temporal = _FakeTemporalConfig()


def test_discovery_finds_shards_named_after_temporal_active_embedder(tmp_path):
    """A shard on disk named after the temporal active_embedder ("voyage-context-4")
    must be discovered, even though the regular semantic model is "voyage-code-3"."""
    config = _FakeConfig()

    ts = datetime(2024, 5, 15, tzinfo=timezone.utc)
    shard_name = get_shard_collection_name("voyage-context-4", ts)
    shard_dir = tmp_path / shard_name
    shard_dir.mkdir(parents=True)
    write_structure_marker(shard_dir, "voyage_context_4")

    groups = _discover_provider_shards_with_pruning(
        config, tmp_path, time_range=None, provider_filter=None
    )

    all_shards = [s for _base, shards in groups for s in shards]
    assert shard_name in all_shards, (
        f"Expected shard {shard_name!r} to be discovered via "
        f"config.temporal.embedders, got groups: {groups}"
    )


def test_discovery_does_not_use_regular_semantic_model_name(tmp_path):
    """A shard named after the REGULAR semantic model ("voyage-code-3") must NOT
    be discovered by temporal recall — only temporal.embedders entries count."""
    config = _FakeConfig()

    ts = datetime(2024, 5, 15, tzinfo=timezone.utc)
    wrong_shard_name = get_shard_collection_name("voyage-code-3", ts)
    shard_dir = tmp_path / wrong_shard_name
    shard_dir.mkdir(parents=True)
    write_structure_marker(shard_dir, "voyage_code_3")

    groups = _discover_provider_shards_with_pruning(
        config, tmp_path, time_range=None, provider_filter=None
    )

    all_shards = [s for _base, shards in groups for s in shards]
    assert wrong_shard_name not in all_shards


def test_provider_for_collection_is_pinned_to_temporal_embedder_model():
    """The embedding provider constructed for a temporal shard must report
    get_current_model() == the temporal embedder name, NOT config.voyage_ai.model."""
    config = _FakeConfig()
    collection_name = "code-indexer-temporal-voyage_context_4-2024Q2"

    provider = _create_embedding_provider_for_collection(config, collection_name)

    assert provider.get_current_model() == "voyage-context-4"
    assert provider.get_current_model() != config.voyage_ai.model
