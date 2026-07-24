"""execute_temporal_query_with_fusion's end-to-end resolver threading
(Story #1457 AC8 dispatch consumption contract items 1-6, all together).

When a resolver is passed into the top-level dispatch entry point, it
flows: discovery (_discover_provider_shards_with_pruning ->
resolve_overlapping_shards, is_queryable pre-filtered) -> the shard loop
(_query_shards_raw, each read wrapped in resolver.pin(...), eviction keyed
by the resolved path) -- the FULL chain, proven together for the first
time. resolver=None (every current production caller) remains completely
unaffected.

Per this module's established test convention: _query_single_provider is
stubbed (real embedding/HNSW loads); the resolver/pin/discovery machinery
itself -- the actual SUT here -- uses REAL TemporalShardResolver,
QueryTracker, AliasManager. No mocking of the code under test.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from code_indexer.config import VoyageAIConfig
from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.services.temporal.temporal_fusion_dispatch import (
    execute_temporal_query_with_fusion,
)
from code_indexer.services.temporal.temporal_search_service import (
    ALL_TIME_RANGE,
    TemporalSearchResults,
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


def _make_vs(tmp_path: Path, cache) -> MagicMock:
    vs = MagicMock()
    vs.project_root = tmp_path
    vs.base_path = tmp_path / "index"
    vs.hnsw_index_cache = cache
    vs.memory_governor = None
    return vs


def _stub_query_single_provider(cfg, vs_, coll_name, *a, **kw):
    return TemporalSearchResults(
        results=[], query="q", filter_type="none", filter_value=None, total_found=0
    )


def test_resolver_threads_through_discovery_and_pin_end_to_end(tmp_path):
    """A published sister quarter, discovered via resolve_overlapping_shards
    and read inside a resolver.pin() block -- eviction keyed by the
    RESOLVED sister path, proving the full chain works together."""
    config = _FakeConfig()

    aliases_dir = tmp_path / "aliases"
    sister_root = tmp_path / "sister"
    legacy_index_path = tmp_path / "index"
    alias_manager = AliasManager(str(aliases_dir))

    version_dir = (
        sister_root
        / ".versioned"
        / "evolution-temporal-voyage_code_3-2024Q1"
        / "v_1700000000"
    )
    version_dir.mkdir(parents=True)
    alias_manager.create_alias(
        "evolution-temporal-voyage_code_3-2024Q1", str(version_dir)
    )

    query_tracker = QueryTracker()
    resolver = TemporalShardResolver(
        alias_manager=alias_manager,
        repo_alias="evolution",
        sister_root=sister_root,
        legacy_index_path=legacy_index_path,
        query_tracker=query_tracker,
    )

    cache = MagicMock()
    vs = _make_vs(tmp_path, cache)
    vs.base_path = legacy_index_path

    with patch(
        "code_indexer.services.temporal.temporal_fusion_dispatch._query_single_provider",
        side_effect=_stub_query_single_provider,
    ):
        result = execute_temporal_query_with_fusion(
            config=config,
            index_path=legacy_index_path,
            vector_store=vs,
            query_text="q",
            limit=10,
            time_range=ALL_TIME_RANGE,
            resolver=resolver,
        )

    assert result.shards_total == 1
    assert result.shards_attempted == 1
    assert result.shards_succeeded == 1

    # Eviction keyed by the RESOLVED sister path -- proving pin wiring
    # actually fired, not the legacy base_path/shard_name reconstruction.
    expected_key = str(version_dir.resolve())
    cache.invalidate.assert_called_once_with(expected_key)
    assert query_tracker.get_ref_count(expected_key) == 0


def test_resolver_none_default_matches_no_resolver_baseline_exactly(tmp_path):
    """resolver=None (the default -- every current production caller) must
    produce the EXACT SAME TemporalSearchResults as calling the function
    with no resolver argument at all -- proving the resolver param addition
    changed nothing about the no-resolver code path, field for field."""
    config = _FakeConfig()

    baseline_vs = _make_vs(tmp_path, MagicMock())
    baseline = execute_temporal_query_with_fusion(
        config=config,
        index_path=tmp_path / "index",
        vector_store=baseline_vs,
        query_text="q",
        limit=10,
    )

    explicit_none_vs = _make_vs(tmp_path, MagicMock())
    explicit_none = execute_temporal_query_with_fusion(
        config=config,
        index_path=tmp_path / "index",
        vector_store=explicit_none_vs,
        query_text="q",
        limit=10,
        resolver=None,
    )

    assert explicit_none.results == baseline.results == []
    assert explicit_none.warning == baseline.warning
    assert explicit_none.warning is not None
    assert explicit_none.filter_type == baseline.filter_type
    assert explicit_none.filter_value == baseline.filter_value
    assert explicit_none.shards_total == baseline.shards_total
    assert explicit_none.shards_attempted == baseline.shards_attempted
    assert explicit_none.shards_succeeded == baseline.shards_succeeded


class _AlwaysSwapQueryTracker(QueryTracker):
    """Performs a REAL alias swap on EVERY increment_ref call, cycling
    through swap_targets -- simulating a persistently racing pointer to
    deterministically exercise the bounded pin-exhaustion path (mirrors
    test_temporal_fusion_dispatch_pin_wiring_1457.py's established
    double)."""

    def __init__(
        self, alias_manager: AliasManager, pointer_namespace: str, swap_targets: list
    ) -> None:
        super().__init__()
        self._alias_manager = alias_manager
        self._pointer_namespace = pointer_namespace
        self._swap_targets = iter(swap_targets)

    def increment_ref(self, index_path: str) -> None:
        new_target = next(self._swap_targets, None)
        if new_target is not None:
            old_target = self._alias_manager.read_alias(self._pointer_namespace)
            self._alias_manager.swap_alias(
                self._pointer_namespace, new_target, old_target
            )
        super().increment_ref(index_path)


def test_pin_exhaustion_surfaces_as_explicit_warning(tmp_path):
    """Story #1457 HIGH #6 (2026-07-23 code review): pin exhaustion must
    surface as an explicit .warning on the response -- never silently
    degrade to partial results with no signal at all."""
    config = _FakeConfig()

    aliases_dir = tmp_path / "aliases"
    sister_root = tmp_path / "sister"
    legacy_index_path = tmp_path / "index"
    alias_manager = AliasManager(str(aliases_dir))

    pointer_namespace = "evolution-temporal-voyage_code_3-2024Q1"
    versions = []
    for i in range(4):
        v = sister_root / ".versioned" / pointer_namespace / f"v_170000000{i}"
        v.mkdir(parents=True)
        versions.append(v)
    alias_manager.create_alias(pointer_namespace, str(versions[0]))

    tracker = _AlwaysSwapQueryTracker(
        alias_manager, pointer_namespace, [str(v) for v in versions[1:]]
    )
    resolver = TemporalShardResolver(
        alias_manager=alias_manager,
        repo_alias="evolution",
        sister_root=sister_root,
        legacy_index_path=legacy_index_path,
        query_tracker=tracker,
    )

    vs = _make_vs(tmp_path, MagicMock())
    vs.base_path = legacy_index_path

    with patch(
        "code_indexer.services.temporal.temporal_fusion_dispatch._query_single_provider",
        side_effect=_stub_query_single_provider,
    ):
        result = execute_temporal_query_with_fusion(
            config=config,
            index_path=legacy_index_path,
            vector_store=vs,
            query_text="q",
            limit=10,
            time_range=ALL_TIME_RANGE,
            resolver=resolver,
        )

    assert result.shards_attempted == 1
    assert result.shards_succeeded == 0
    assert result.warning is not None
    assert "pin exhaustion" in result.warning
