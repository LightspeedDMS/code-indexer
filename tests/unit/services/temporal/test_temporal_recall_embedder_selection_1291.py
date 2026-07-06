"""Recall embedder selection (Story #1291 AC7/AC8/AC9).

Query resolution must select EXACTLY ONE embedder's collections per query:
- Omitted temporal_embedder override -> active_embedder's collections.
- Explicit temporal_embedder override -> THAT embedder's collections, and if
  it has no v2 collections, an EMPTY/typed "not indexed" result -- NEVER a
  silent fallback to active_embedder.
- No cross-embedder fusion, ever (at most one embedder's shards are queried
  per call -- this is a hard invariant, not merely a happy-path behavior).
"""

from datetime import datetime, timezone

import pytest

from code_indexer.config import VoyageAIConfig
from code_indexer.services.temporal.temporal_collection_naming import (
    get_shard_collection_name,
)
from code_indexer.services.temporal.temporal_fusion_dispatch import (
    _discover_provider_shards_with_pruning,
    execute_temporal_query_with_fusion,
)
from code_indexer.services.temporal.temporal_structure_marker import (
    write_structure_marker,
)


class _FakeTemporalConfig:
    embedders = ["voyage-context-4", "embed-v4.0"]
    active_embedder = "voyage-context-4"
    aggregation_chunk_chars = 4096
    diff_context_lines = 5


class _FakeConfig:
    def __init__(self) -> None:
        self.voyage_ai = VoyageAIConfig(model="voyage-code-3")
        self.embedding_provider = "voyage-ai"
        self.temporal = _FakeTemporalConfig()


def _write_shard(index_path, embedder_name: str, model_slug: str, ts: datetime):
    shard_name = get_shard_collection_name(embedder_name, ts)
    shard_dir = index_path / shard_name
    shard_dir.mkdir(parents=True, exist_ok=True)
    write_structure_marker(shard_dir, model_slug)
    return shard_name


class TestDiscoveryDefaultsToActiveEmbedderOnly:
    """AC7: an OMITTED temporal_embedder override uses active_embedder only."""

    def test_omitted_override_resolves_only_active_embedder_shards(self, tmp_path):
        config = _FakeConfig()
        ts = datetime(2024, 5, 15, tzinfo=timezone.utc)
        voyage_shard = _write_shard(
            tmp_path, "voyage-context-4", "voyage_context_4", ts
        )
        cohere_shard = _write_shard(tmp_path, "embed-v4.0", "embed_v4_0", ts)

        groups = _discover_provider_shards_with_pruning(
            config, tmp_path, time_range=None, provider_filter=None
        )

        assert len(groups) == 1, f"expected exactly one embedder group, got {groups}"
        all_shards = [s for _base, shards in groups for s in shards]
        assert voyage_shard in all_shards
        assert cohere_shard not in all_shards


class TestDiscoveryExplicitOverride:
    """AC7/AC8: an explicit temporal_embedder override selects THAT embedder,
    never silently falling back to active_embedder."""

    def test_explicit_override_resolves_only_named_embedder_shards(self, tmp_path):
        config = _FakeConfig()
        ts = datetime(2024, 5, 15, tzinfo=timezone.utc)
        voyage_shard = _write_shard(
            tmp_path, "voyage-context-4", "voyage_context_4", ts
        )
        cohere_shard = _write_shard(tmp_path, "embed-v4.0", "embed_v4_0", ts)

        groups = _discover_provider_shards_with_pruning(
            config,
            tmp_path,
            time_range=None,
            provider_filter=None,
            temporal_embedder="embed-v4.0",
        )

        assert len(groups) == 1, f"expected exactly one embedder group, got {groups}"
        all_shards = [s for _base, shards in groups for s in shards]
        assert cohere_shard in all_shards
        assert voyage_shard not in all_shards

    def test_explicit_override_with_no_collections_returns_empty_not_fallback(
        self, tmp_path
    ):
        """AC8: overriding to an embedder with ZERO v2 collections must NOT
        silently redirect to active_embedder's (existing) collections."""
        config = _FakeConfig()
        ts = datetime(2024, 5, 15, tzinfo=timezone.utc)
        # Only the ACTIVE embedder (voyage-context-4) has a shard on disk.
        _write_shard(tmp_path, "voyage-context-4", "voyage_context_4", ts)

        groups = _discover_provider_shards_with_pruning(
            config,
            tmp_path,
            time_range=None,
            provider_filter=None,
            temporal_embedder="embed-v4.0",
        )

        assert groups == [], (
            "an explicit override to an unindexed embedder must resolve to "
            "ZERO shard groups -- never silently fall back to active_embedder"
        )


class TestExecuteQueryTypedEmptyOnUnindexedOverride:
    """AC8: the full query entry point returns a typed 'not indexed' warning
    (never silently substitutes active_embedder's results) for an explicit
    override with no collections."""

    def test_explicit_override_unindexed_embedder_returns_typed_warning(self, tmp_path):
        from unittest.mock import MagicMock, patch

        config = _FakeConfig()
        ts = datetime(2024, 5, 15, tzinfo=timezone.utc)
        _write_shard(tmp_path, "voyage-context-4", "voyage_context_4", ts)

        vector_store = MagicMock()
        vector_store.project_root = tmp_path

        with patch(
            "code_indexer.services.temporal.temporal_migration.migrate_legacy_temporal_collection"
        ):
            result = execute_temporal_query_with_fusion(
                config=config,
                index_path=tmp_path,
                vector_store=vector_store,
                query_text="anything",
                limit=5,
                temporal_embedder="embed-v4.0",
            )

        assert result.results == []
        assert result.warning is not None
        assert "embed-v4.0" in result.warning


class TestNoCrossEmbedderFusionInvariant:
    """AC9: the dispatch layer must NEVER attempt to fuse results across more
    than one embedder's shard group -- proven as a hard invariant, not just a
    happy-path assertion (discovery is defensively assumed to return at most
    one group; if that invariant is ever violated upstream, the dispatch
    layer fails loud rather than silently mixing providers)."""

    def test_more_than_one_provider_group_fails_loud_never_fuses(self, tmp_path):
        from unittest.mock import MagicMock, patch

        config = _FakeConfig()
        vector_store = MagicMock()
        vector_store.project_root = tmp_path

        two_groups = [
            (
                "code-indexer-temporal-voyage_context_4",
                ["code-indexer-temporal-voyage_context_4-2024Q2"],
            ),
            (
                "code-indexer-temporal-embed_v4_0",
                ["code-indexer-temporal-embed_v4_0-2024Q2"],
            ),
        ]

        with (
            patch(
                "code_indexer.services.temporal.temporal_fusion_dispatch._discover_provider_shards_with_pruning",
                return_value=two_groups,
            ),
            patch(
                "code_indexer.services.temporal.temporal_fusion_dispatch.filter_healthy_temporal_providers",
                side_effect=lambda cols: (cols, []),
            ),
            patch(
                "code_indexer.services.temporal.temporal_migration.migrate_legacy_temporal_collection",
            ),
        ):
            with pytest.raises(RuntimeError, match="[Cc]ross-embedder|invariant"):
                execute_temporal_query_with_fusion(
                    config=config,
                    index_path=tmp_path,
                    vector_store=vector_store,
                    query_text="query",
                    limit=5,
                )
