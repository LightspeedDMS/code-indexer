"""_discover_provider_shards_with_pruning's resolver-aware discovery wiring
(Story #1457 AC8 dispatch consumption contract items 1-2).

When a resolver is injected, discovery routes through
resolve_overlapping_shards() (which reuses TemporalShardResolver.catalog()/
.resolve() -- the pointer-first union catalog, AC8's discovery seam) instead
of the bare index_path.iterdir() scan get_overlapping_shards() performs, and
filters out any resolved shard with is_queryable=False BEFORE it is ever
returned to the caller (so an unqueryable IN_REPO_LEGACY shard -- row-
bearing but with no hnsw_index.bin -- is never attempted).

resolver=None (every current production caller) is BYTE-IDENTICAL to today
-- proven by the full pre-existing test_temporal_recall_embedder_selection
suite passing unchanged.

Real AliasManager, real TemporalShardResolver -- no mocking of the code
under test.
"""

from __future__ import annotations

from datetime import datetime, timezone

from code_indexer.config import VoyageAIConfig
from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.services.temporal.temporal_fusion_dispatch import (
    _discover_provider_shards_with_pruning,
)
from code_indexer.services.temporal.temporal_shard_resolver import (
    TemporalShardResolver,
)


class _FakeTemporalConfig:
    embedders = ["voyage-code-3"]
    active_embedder = "voyage-code-3"
    aggregation_chunk_chars = 4096
    diff_context_lines = 5


class _FakeConfig:
    def __init__(self) -> None:
        self.voyage_ai = VoyageAIConfig(model="voyage-code-3")
        self.embedding_provider = "voyage-ai"
        self.temporal = _FakeTemporalConfig()


def _make_resolver(tmp_path, repo_alias="evolution") -> TemporalShardResolver:
    aliases_dir = tmp_path / "aliases"
    sister_root = tmp_path / "sister"
    legacy_index_path = tmp_path / "index"
    alias_manager = AliasManager(str(aliases_dir))
    return TemporalShardResolver(
        alias_manager=alias_manager,
        repo_alias=repo_alias,
        sister_root=sister_root,
        legacy_index_path=legacy_index_path,
    )


def test_resolver_none_is_byte_identical_default(tmp_path):
    """Default (no resolver): behaves exactly like today -- zero shards on
    disk resolves to zero groups (existing iterdir-based behavior)."""
    config = _FakeConfig()
    groups = _discover_provider_shards_with_pruning(
        config, tmp_path / "index", time_range=None, provider_filter=None
    )
    assert groups == []


def test_resolver_discovers_via_sister_pointer_not_iterdir(tmp_path):
    """A resolver injected, with a published sister quarter and NOTHING on
    the legacy index_path -- discovery must still find it (proving it
    routes through the resolver's catalog, not an iterdir scan of
    index_path, which is empty here)."""
    config = _FakeConfig()
    resolver = _make_resolver(tmp_path)
    version_dir = (
        resolver._sister_root
        / ".versioned"
        / "evolution-temporal-voyage_code_3-2024Q1"
        / "v_1700000000"
    )
    version_dir.mkdir(parents=True)
    resolver._alias_manager.create_alias(
        "evolution-temporal-voyage_code_3-2024Q1", str(version_dir)
    )

    groups = _discover_provider_shards_with_pruning(
        config,
        tmp_path / "index",  # legacy_index_path -- empty, nothing here
        time_range=None,
        provider_filter=None,
        resolver=resolver,
    )

    assert len(groups) == 1
    base_name, shards = groups[0]
    assert shards == ["code-indexer-temporal-voyage_code_3-2024Q1"]


def test_resolver_excludes_non_queryable_in_repo_legacy_shard(tmp_path):
    """An IN_REPO_LEGACY shard with real rows but NO hnsw_index.bin
    (is_queryable=False) must be excluded from the returned set entirely --
    never attempted, per the dispatch consumption contract's pre-query
    filtering requirement."""
    config = _FakeConfig()
    resolver = _make_resolver(tmp_path, repo_alias="evolution")

    legacy_shard = (
        resolver._legacy_index_path / "code-indexer-temporal-voyage_code_3-2024Q1"
    )
    legacy_shard.mkdir(parents=True)
    (legacy_shard / "vector_abc.json").write_text('{"id": "p1"}')
    # NOTE: no hnsw_index.bin written -- is_queryable must be False.

    groups = _discover_provider_shards_with_pruning(
        config,
        resolver._legacy_index_path,
        time_range=None,
        provider_filter=None,
        resolver=resolver,
    )

    assert groups == []


def test_resolver_date_range_filtering_still_applies(tmp_path):
    config = _FakeConfig()
    resolver = _make_resolver(tmp_path)
    for suffix in ["2024Q1", "2024Q2"]:
        version_dir = (
            resolver._sister_root
            / ".versioned"
            / f"evolution-temporal-voyage_code_3-{suffix}"
            / "v_1700000000"
        )
        version_dir.mkdir(parents=True)
        resolver._alias_manager.create_alias(
            f"evolution-temporal-voyage_code_3-{suffix}", str(version_dir)
        )

    start = datetime(2024, 4, 1, tzinfo=timezone.utc).strftime("%Y-%m-%d")
    end = datetime(2024, 6, 30, tzinfo=timezone.utc).strftime("%Y-%m-%d")

    groups = _discover_provider_shards_with_pruning(
        config,
        tmp_path / "index",
        time_range=(start, end),
        provider_filter=None,
        resolver=resolver,
    )

    assert len(groups) == 1
    _base, shards = groups[0]
    assert shards == ["code-indexer-temporal-voyage_code_3-2024Q2"]
