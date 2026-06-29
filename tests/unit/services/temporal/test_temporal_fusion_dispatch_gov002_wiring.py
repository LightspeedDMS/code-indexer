"""Story 4 Critical 1 — GOV-002 emitted from temporal_fusion_dispatch evict site.

The dispatch RED evict path MUST call gov.log_gov002_evict() after each shard
invalidation so GOV-002 lines appear in logs.db.  This test asserts the
call-site wiring, not the log method in isolation.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch


from code_indexer.server.services.memory_governor import MemoryBand, MemoryGovernor
from code_indexer.services.temporal.temporal_fusion_dispatch import _query_shards_raw

# ---------------------------------------------------------------------------
# Named constants
# ---------------------------------------------------------------------------

BYTES_PER_GIB = 1024 * 1024 * 1024
HOST_8_GIB = 8 * BYTES_PER_GIB
PERCENT_DENOMINATOR = 100
NO_SWAP_PAGES_IN = 0
NO_RED_DWELL_SECONDS = 0.0

RED_USAGE_PCT = 90.0
YELLOW_PCT = 70.0
RED_PCT = 85.0
HYSTERESIS_PCT = 10.0

SHARD_NAMES = ["s_2023Q1", "s_2023Q2"]
SHARD_COUNT = len(SHARD_NAMES)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_readers(used_pct: float) -> MagicMock:
    readers = MagicMock()
    vm = MagicMock()
    vm.total = HOST_8_GIB
    vm.used = int(HOST_8_GIB * used_pct / PERCENT_DENOMINATOR)
    readers.read_host_memory.return_value = vm
    readers.read_cgroup_v2_max.side_effect = FileNotFoundError
    readers.read_cgroup_v1_limit.side_effect = FileNotFoundError
    readers.read_pswpin.return_value = NO_SWAP_PAGES_IN
    return readers


def _red_gov() -> MemoryGovernor:
    gov = MemoryGovernor(
        readers=_make_readers(RED_USAGE_PCT),
        enabled=True,
        start_sampler=False,
        yellow_pct=YELLOW_PCT,
        red_pct=RED_PCT,
        hysteresis_pct=HYSTERESIS_PCT,
        red_min_dwell_seconds=NO_RED_DWELL_SECONDS,
    )
    gov._tick()
    assert gov.band == MemoryBand.RED
    return gov


def _make_vs(tmp_path: Path, cache: MagicMock, governor: MemoryGovernor) -> MagicMock:
    vs = MagicMock()
    vs.project_root = tmp_path
    vs.base_path = tmp_path / ".code-indexer" / "index"
    vs.hnsw_index_cache = cache
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
# Tests
# ---------------------------------------------------------------------------


class TestGov002DispatchWiring:
    """GOV-002 must be emitted FROM the temporal_fusion_dispatch evict call-site."""

    def test_gov002_called_from_dispatch_evict_site(self, tmp_path):
        """RED band dispatch evict must call gov.log_gov002_evict() once per shard.

        This is a CALL-SITE test: we spy on the governor's method to verify the
        wiring in temporal_fusion_dispatch.py, not the method itself.
        """
        gov = _red_gov()
        cache = MagicMock()
        vs = _make_vs(tmp_path, cache, gov)

        call_count = [0]
        original = gov.log_gov002_evict

        def _spy(**kwargs):
            call_count[0] += 1
            original(**kwargs)

        gov.log_gov002_evict = _spy  # type: ignore[method-assign]

        _run_shards(vs, SHARD_NAMES)

        assert call_count[0] == SHARD_COUNT, (
            f"Expected gov.log_gov002_evict called {SHARD_COUNT} times (once per shard), "
            f"got {call_count[0]}.  GOV-002 is not wired at the dispatch evict call-site."
        )
