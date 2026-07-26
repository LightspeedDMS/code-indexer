"""_query_shards_raw's resolution-scope pin wiring (Story #1457 AC8 Step 6,
dispatch consumption contract items 3-6).

Makes the AC8 Step 6 pin PRIMITIVE (temporal_shard_resolver.py, already
implemented and tested in isolation) load-bearing: when a resolver is
injected, each shard read is wrapped in `with resolver.pin(...)`, HNSW
eviction is keyed by the PINNED resolved path (not a base_path/shard_name
reconstruction), and the collection name passed downstream is the resolved
object's `.physical_name`.

`resolver=None` (the default -- every current production caller, since
AC1/AC2's live wiring does not exist yet) is BYTE-IDENTICAL to today: no
pin, eviction keyed by `Path(vector_store.base_path) / shard_name` exactly
as Bug #1171 established.

Per this file's established sibling convention
(test_temporal_fusion_dispatch_governor.py): `_query_single_provider` is a
collaborator external to the SUT (it invokes real embedding providers and
loads HNSW indexes) and is stubbed. The pin mechanism itself -- the actual
SUT of these tests -- uses a REAL TemporalShardResolver, REAL QueryTracker,
REAL AliasManager. No mocking of the code under test.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from code_indexer.global_repos.alias_manager import AliasManager
from code_indexer.global_repos.query_tracker import QueryTracker
from code_indexer.services.temporal.temporal_fusion_dispatch import _query_shards_raw
from code_indexer.services.temporal.temporal_search_service import (
    TemporalSearchResults,
)
from code_indexer.services.temporal.temporal_shard_resolver import (
    TemporalShardResolver,
)


def _make_vs(tmp_path: Path, hnsw_cache) -> MagicMock:
    vs = MagicMock()
    vs.project_root = tmp_path
    vs.base_path = tmp_path / "index"
    vs.hnsw_index_cache = hnsw_cache
    vs.memory_governor = None
    return vs


def _stub_query_single_provider(cfg, vs_, coll_name, *a, **kw):
    return TemporalSearchResults(
        results=[], query="q", filter_type="none", filter_value=None, total_found=0
    )


class _AlwaysSwapQueryTracker(QueryTracker):
    """Performs a REAL alias swap on EVERY increment_ref call, cycling
    through swap_targets -- simulating a persistently racing pointer to
    deterministically exercise the bounded pin-exhaustion path."""

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


def test_resolver_none_is_byte_identical_to_bug1171_eviction_key(tmp_path):
    """Default (no resolver): eviction key is EXACTLY
    Path(vector_store.base_path) / shard_name, per Bug #1171 -- unchanged."""
    cache = MagicMock()
    vs = _make_vs(tmp_path, cache)
    config = MagicMock()
    shard_name = "code-indexer-temporal-voyage_code_3-2024Q1"

    with patch(
        "code_indexer.services.temporal.temporal_fusion_dispatch._query_single_provider",
        side_effect=_stub_query_single_provider,
    ):
        _query_shards_raw(config, vs, [shard_name], "q", 30, None, None)

    expected_key = str((vs.base_path / shard_name).resolve())
    cache.invalidate.assert_called_once_with(expected_key)


def test_resolver_wraps_read_in_pin_and_evicts_by_resolved_sister_path(tmp_path):
    """A resolver injected: the shard read is pinned, and HNSW eviction is
    keyed by the RESOLVED sister path (not a base_path reconstruction)."""
    aliases_dir = tmp_path / "aliases"
    sister_root = tmp_path / "sister"
    legacy_index_path = tmp_path / "index"
    alias_manager = AliasManager(str(aliases_dir))

    version_dir = sister_root / ".versioned" / "ns" / "v_1700000000"
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
    config = MagicMock()
    shard_name = "code-indexer-temporal-voyage_code_3-2024Q1"

    with patch(
        "code_indexer.services.temporal.temporal_fusion_dispatch._query_single_provider",
        side_effect=_stub_query_single_provider,
    ):
        _query_shards_raw(
            config, vs, [shard_name], "q", 30, None, None, resolver=resolver
        )

    # Eviction keyed by the RESOLVED sister path -- not base_path/shard_name.
    expected_key = str(version_dir.resolve())
    cache.invalidate.assert_called_once_with(expected_key)

    # Pin released after the read: refcount back to 0.
    assert query_tracker.get_ref_count(expected_key) == 0


def test_resolver_pin_exhaustion_does_not_record_provider_failure(tmp_path):
    """A persistently racing pointer exhausts the pin's bounded retry budget
    -- the shard is NOT counted as succeeded, but record_temporal_failure
    must NOT be called (pin exhaustion is kept OUT of the provider circuit
    breaker, per the spec)."""
    aliases_dir = tmp_path / "aliases"
    sister_root = tmp_path / "sister"
    legacy_index_path = tmp_path / "index"
    alias_manager = AliasManager(str(aliases_dir))

    versions = []
    for i in range(4):
        v = sister_root / ".versioned" / "ns" / f"v_170000000{i}"
        v.mkdir(parents=True)
        versions.append(v)
    pointer_namespace = "evolution-temporal-voyage_code_3-2024Q1"
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
    config = MagicMock()
    shard_name = "code-indexer-temporal-voyage_code_3-2024Q1"

    with (
        patch(
            "code_indexer.services.temporal.temporal_fusion_dispatch._query_single_provider",
            side_effect=_stub_query_single_provider,
        ),
        patch(
            "code_indexer.services.temporal.temporal_fusion_dispatch.record_temporal_failure"
        ) as mock_failure,
        patch(
            "code_indexer.services.temporal.temporal_fusion_dispatch.record_temporal_pin_exhaustion"
        ) as mock_exhausted,
    ):
        (
            results_by_shard,
            shards_attempted,
            shards_succeeded,
            pin_exhausted_shards,
        ) = _query_shards_raw(
            config, vs, [shard_name], "q", 30, None, None, resolver=resolver
        )

    assert shards_attempted == 1
    assert shards_succeeded == 0
    assert results_by_shard == {}
    mock_failure.assert_not_called()
    mock_exhausted.assert_called_once()
    assert pin_exhausted_shards == [shard_name], (
        "pin exhaustion must be tracked as an explicit, caller-visible "
        "signal (Story #1457 HIGH #6) -- not just a log line"
    )
