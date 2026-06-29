"""Fail-safe eviction tests for MemoryGovernor call-site (Story #1213 Story 3).

Covers:
- disabled (enabled=False): evicts (== #1171 safe baseline).
- signal-init-failed (band stays RED after reader error): evicts.
- should_evict_after_shard() raising: call site catches and evicts (anti-silent-failure).
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

from code_indexer.services.temporal.temporal_fusion_dispatch import _query_shards_raw
from code_indexer.server.services.memory_governor import MemoryBand, MemoryGovernor


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


def _run_shards(vs: MagicMock, shards: list) -> None:
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
# Test class: disabled + signal-init-failed + exception fail-safe
# ---------------------------------------------------------------------------


class TestDisabledAndSignalInitFailedEvicts:
    """Disabled, signal-init-failed, and exception-throwing governors must all evict."""

    def test_disabled_governor_evicts(self, tmp_path):
        """enabled=False -> should_evict returns True -> invalidate called per shard."""
        gov = MemoryGovernor(
            readers=MagicMock(),
            enabled=False,
            start_sampler=False,
            yellow_pct=70.0,
            red_pct=85.0,
            hysteresis_pct=10.0,
            red_min_dwell_seconds=0.0,
        )
        assert gov.should_evict_after_shard() is True  # Prerequisite
        cache = MagicMock()
        vs = _make_vs(tmp_path, cache, governor=gov)

        _run_shards(vs, ["s_2023Q1", "s_2023Q2"])

        assert cache.invalidate.call_count == 2

    def test_signal_init_failed_governor_evicts(self, tmp_path):
        """Signal-init-failed: band stays RED (fail-safe pre-init) -> evicts."""
        readers = MagicMock()
        readers.read_host_memory.side_effect = OSError("no signal")
        readers.read_cgroup_v2_max.side_effect = FileNotFoundError
        readers.read_cgroup_v1_limit.side_effect = FileNotFoundError
        readers.read_pswpin.side_effect = OSError("no signal")

        gov = MemoryGovernor(
            readers=readers,
            enabled=True,
            start_sampler=False,
            yellow_pct=70.0,
            red_pct=85.0,
            hysteresis_pct=10.0,
            red_min_dwell_seconds=0.0,
        )
        gov._tick()  # Reader throws -> fail-safe RED applied
        assert gov.band == MemoryBand.RED

        cache = MagicMock()
        vs = _make_vs(tmp_path, cache, governor=gov)

        _run_shards(vs, ["s_2023Q1"])

        cache.invalidate.assert_called_once()

    def test_exception_from_should_evict_defaults_to_evict(self, tmp_path):
        """should_evict_after_shard() raising -> call site catches it and evicts."""
        gov = MagicMock(spec=MemoryGovernor)
        gov.should_evict_after_shard.side_effect = RuntimeError("governor exploded")
        gov.maybe_trim = MagicMock()
        cache = MagicMock()
        vs = _make_vs(tmp_path, cache, governor=gov)
        shards = ["s_2023Q1", "s_2023Q2"]

        # Must NOT raise — the call site swallows the governor exception
        _run_shards(vs, shards)

        # Fail-safe: evicted (not silently retained)
        assert cache.invalidate.call_count == len(shards)
