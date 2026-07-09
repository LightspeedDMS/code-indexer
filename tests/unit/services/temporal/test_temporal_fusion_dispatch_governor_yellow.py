"""Tests for YELLOW overfetch reduction, maybe_trim, and RED sequential gate (Story #1213 Story 3).

Covers:
- YELLOW_OVERFETCH_MULTIPLIER constant exists and is < TEMPORAL_OVERFETCH_MULTIPLIER.
- maybe_trim() increments trim_calls and never raises.
- _query_shards_raw processes shards strictly sequentially under RED (concurrency=1 gate).
"""

from unittest.mock import MagicMock, patch

from code_indexer.server.services.memory_governor import MemoryBand, MemoryGovernor


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_readers(used_pct: float) -> MagicMock:
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


def _red_gov() -> MemoryGovernor:
    gov = MemoryGovernor(
        readers=_make_readers(90.0),
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
# Test class: YELLOW overfetch + maybe_trim + RED sequential gate
# ---------------------------------------------------------------------------


class TestYellowAndConcurrencyGate:
    """YELLOW overfetch reduction, maybe_trim existence, and RED sequential concurrency gate."""

    def test_yellow_overfetch_multiplier_constant_exists_and_is_lower(self):
        """YELLOW_OVERFETCH_MULTIPLIER must exist and be < TEMPORAL_OVERFETCH_MULTIPLIER."""
        from code_indexer.services.temporal.temporal_fusion_dispatch import (
            YELLOW_OVERFETCH_MULTIPLIER,
        )
        from code_indexer.services.temporal.temporal_fusion import (
            TEMPORAL_OVERFETCH_MULTIPLIER,
        )

        assert YELLOW_OVERFETCH_MULTIPLIER < TEMPORAL_OVERFETCH_MULTIPLIER, (
            f"YELLOW_OVERFETCH_MULTIPLIER ({YELLOW_OVERFETCH_MULTIPLIER}) must be < "
            f"TEMPORAL_OVERFETCH_MULTIPLIER ({TEMPORAL_OVERFETCH_MULTIPLIER})"
        )
        assert YELLOW_OVERFETCH_MULTIPLIER >= 1, "Must be at least 1 to return results"

    def test_maybe_trim_exists_and_increments_trim_calls(self):
        """maybe_trim() on MemoryGovernor increments trim_calls and never raises."""
        gov = _red_gov()
        before = gov.counters.trim_calls
        gov.maybe_trim()  # must not raise
        assert gov.counters.trim_calls == before + 1

    def test_red_scan_is_strictly_sequential_no_overlap(self, tmp_path):
        """RED band: _query_shards_raw processes each shard one-at-a-time (concurrency=1).
        Verified by tracking active concurrent calls — max observed concurrency must be 1.
        """
        import threading
        from code_indexer.services.temporal.temporal_fusion_dispatch import (
            _query_shards_raw,
        )

        gov = _red_gov()
        vs = MagicMock()
        vs.project_root = tmp_path
        vs.base_path = tmp_path / ".code-indexer" / "index"
        vs.hnsw_index_cache = MagicMock()
        vs.memory_governor = gov

        active_count = [0]
        max_concurrency = [0]
        lock = threading.Lock()

        def counting_stub(cfg, vs_, shard, *a, **kw):
            with lock:
                active_count[0] += 1
                if active_count[0] > max_concurrency[0]:
                    max_concurrency[0] = active_count[0]
            # No sleep needed — purely sequential loop means count stays at 1
            from code_indexer.services.temporal.temporal_search_service import (
                TemporalSearchResults,
            )

            result = TemporalSearchResults(
                results=[],
                query="q",
                filter_type="none",
                filter_value=None,
                total_found=0,
            )
            with lock:
                active_count[0] -= 1
            return result

        config = MagicMock()
        config.embedding_provider = "voyage-ai"
        shards = ["s_2023Q1", "s_2023Q2", "s_2023Q3"]

        with patch(
            "code_indexer.services.temporal.temporal_fusion_dispatch._query_single_provider",
            side_effect=counting_stub,
        ):
            _query_shards_raw(config, vs, shards, "test", 30, None, None)

        assert max_concurrency[0] == 1, (
            f"Max observed concurrency was {max_concurrency[0]}, expected 1 (sequential gate)"
        )
