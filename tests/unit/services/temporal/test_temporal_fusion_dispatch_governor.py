"""Tests for MemoryGovernor call-site integration in temporal_fusion_dispatch (Story #1213 Story 3).

The SUT is `_query_shards_raw`'s finally-block eviction logic.
`_query_single_provider` is a collaborator external to the eviction SUT:
it invokes real embedding providers (VoyageAI/Cohere) and loads HNSW indexes,
so it is mocked to keep these unit tests dependency-free.
"""

from pathlib import Path
from unittest.mock import MagicMock, call, patch

from code_indexer.server.services.memory_governor import MemoryBand, MemoryGovernor
from code_indexer.services.temporal.temporal_fusion_dispatch import _query_shards_raw


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------


def _make_vs(tmp_path: Path, hnsw_cache, governor=None) -> MagicMock:
    vs = MagicMock()
    vs.project_root = tmp_path
    vs.base_path = tmp_path / ".code-indexer" / "index"
    vs.hnsw_index_cache = hnsw_cache
    vs.memory_governor = governor
    return vs


def _expected_key(base_path: Path, shard: str) -> str:
    """Bug #1171 proven invalidate key: str((base_path / shard).resolve())."""
    return str((base_path / shard).resolve())


def _run_shards(vs: MagicMock, shards: list) -> None:
    """Drive _query_shards_raw with a stubbed _query_single_provider collaborator."""
    config = MagicMock()
    config.embedding_provider = "voyage-ai"

    def _stub(cfg, vs_, shard, *a, **kw):
        from code_indexer.services.temporal.temporal_search_service import (
            TemporalSearchResults,
        )

        return TemporalSearchResults(
            results=[], query="q", filter_type="none", filter_value=None, total_found=0
        )

    with patch(
        "code_indexer.services.temporal.temporal_fusion_dispatch._query_single_provider",
        side_effect=_stub,
    ):
        _query_shards_raw(config, vs, shards, "test", 30, None, None)


# ---------------------------------------------------------------------------
# Test class 1: CLI/solo safety (gov is None) — byte-identical to #1171
# ---------------------------------------------------------------------------


class TestCLISoloEviction:
    """CLI/solo path: memory_governor=None. Must evict exactly as Bug #1171."""

    def test_evicts_every_shard_when_gov_none(self, tmp_path):
        """CLI/solo: governor=None -> invalidate called once per shard (#1171 key)."""
        cache = MagicMock()
        shards = ["s_2023Q1", "s_2023Q2"]
        vs = _make_vs(tmp_path, cache, governor=None)

        _run_shards(vs, shards)

        assert cache.invalidate.call_count == len(shards)
        cache.invalidate.assert_has_calls(
            [call(_expected_key(vs.base_path, s)) for s in shards], any_order=False
        )

    def test_no_crash_when_no_cache(self, tmp_path):
        """CLI/solo: hnsw_index_cache=None -> no crash, no eviction (original #1171)."""
        vs = _make_vs(tmp_path, hnsw_cache=None, governor=None)
        _run_shards(vs, ["s_2023Q1"])  # must not raise


# ---------------------------------------------------------------------------
# Governor helpers used by GREEN/RED/YELLOW/fail-safe tests
# ---------------------------------------------------------------------------


def _fake_readers(used_pct: float = 10.0) -> MagicMock:
    readers = MagicMock()
    total = 8 * 1024 * 1024 * 1024
    vm = MagicMock()
    vm.total = total
    vm.used = int(total * used_pct / 100)
    readers.read_host_memory.return_value = vm
    readers.read_cgroup_v2_max.side_effect = FileNotFoundError
    readers.read_cgroup_v1_limit.side_effect = FileNotFoundError
    readers.read_pswpin.return_value = 0
    return readers


def _green_gov() -> MemoryGovernor:
    gov = MemoryGovernor(
        readers=_fake_readers(10.0),
        enabled=True,
        start_sampler=False,
        yellow_pct=70.0,
        red_pct=85.0,
        hysteresis_pct=10.0,
        red_min_dwell_seconds=0.0,
    )
    gov._tick()
    assert gov.band == MemoryBand.GREEN
    return gov


def _red_gov() -> MemoryGovernor:
    gov = MemoryGovernor(
        readers=_fake_readers(90.0),
        enabled=True,
        start_sampler=False,
        yellow_pct=70.0,
        red_pct=85.0,
        hysteresis_pct=10.0,
        red_min_dwell_seconds=0.0,
    )
    gov._tick()
    assert gov.band == MemoryBand.RED
    return gov


# ---------------------------------------------------------------------------
# Test class 2: Server GREEN — shards MUST be retained
# ---------------------------------------------------------------------------


class TestServerGreenRetains:
    """Server mode GREEN band: invalidate must NOT be called."""

    def test_green_no_eviction(self, tmp_path):
        """GREEN band -> invalidate NOT called -> cross-query reuse possible."""
        cache = MagicMock()
        vs = _make_vs(tmp_path, cache, governor=_green_gov())

        _run_shards(vs, ["s_2023Q1", "s_2023Q2"])

        cache.invalidate.assert_not_called()

    def test_green_trim_counter_stays_zero(self, tmp_path):
        """GREEN band -> trim_calls stays at 0 (no eviction, no trim)."""
        gov = _green_gov()
        vs = _make_vs(tmp_path, MagicMock(), governor=gov)

        _run_shards(vs, ["s_2023Q1"])

        assert gov.counters.trim_calls == 0

    def test_green_evict_counter_stays_zero(self, tmp_path):
        """GREEN band -> shards_evicted_after_use counter unchanged."""
        gov = _green_gov()
        before = gov.counters.shards_evicted_after_use
        vs = _make_vs(tmp_path, MagicMock(), governor=gov)

        _run_shards(vs, ["s_2023Q1", "s_2023Q2"])

        assert gov.counters.shards_evicted_after_use == before


# ---------------------------------------------------------------------------
# Test class 3: Server RED — evicts (== #1171)
# ---------------------------------------------------------------------------


class TestServerRedEvicts:
    """Server mode RED band: must evict per shard, track counters, call maybe_trim."""

    def test_red_evicts_every_shard_with_correct_key(self, tmp_path):
        """RED band -> invalidate called per shard with the proven #1171 key."""
        cache = MagicMock()
        shards = ["s_2023Q1", "s_2023Q2"]
        vs = _make_vs(tmp_path, cache, governor=_red_gov())

        _run_shards(vs, shards)

        assert cache.invalidate.call_count == len(shards)
        for shard in shards:
            cache.invalidate.assert_any_call(_expected_key(vs.base_path, shard))

    def test_red_increments_evicted_counter(self, tmp_path):
        """RED band -> shards_evicted_after_use increments by number of shards."""
        gov = _red_gov()
        before = gov.counters.shards_evicted_after_use
        vs = _make_vs(tmp_path, MagicMock(), governor=gov)
        shards = ["s_2023Q1", "s_2023Q2"]

        _run_shards(vs, shards)

        assert gov.counters.shards_evicted_after_use == before + len(shards)

    def test_red_calls_maybe_trim_per_eviction(self, tmp_path):
        """RED band -> maybe_trim() called once per shard eviction (trim_calls increments)."""
        gov = _red_gov()
        before = gov.counters.trim_calls
        vs = _make_vs(tmp_path, MagicMock(), governor=gov)
        shards = ["s_2023Q1", "s_2023Q2"]

        _run_shards(vs, shards)

        assert gov.counters.trim_calls >= before + len(shards)
