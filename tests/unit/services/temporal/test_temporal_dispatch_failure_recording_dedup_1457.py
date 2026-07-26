"""Failure/success recording must have ONE boundary, not duplicated across
dispatch layers (Story #1457 HIGH #9, 2026-07-23 code review).

Before this fix, BOTH `_query_single_provider` (the lowest layer) and
`_query_shards_raw` (the dispatch loop wrapping it) independently called
`record_temporal_success`/`record_temporal_failure` for the SAME logical
shard-query outcome -- double-counting every temporal query result against
ProviderHealthMonitor's circuit breaker.

The SUT here is the RECORDING BOUNDARY itself, so `_query_single_provider`
is NOT stubbed (that would hide its own recording call) -- only its
deepest external collaborators (the embedding-provider factory and
TemporalSearchService.query_temporal) are stubbed, letting the REAL
`_query_single_provider` code run and reach its own record call, exactly
as production does.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from code_indexer.services.temporal.temporal_fusion_dispatch import _query_shards_raw


def _make_vs(tmp_path: Path) -> MagicMock:
    vs = MagicMock()
    vs.project_root = tmp_path
    vs.base_path = tmp_path / "index"
    vs.hnsw_index_cache = None
    vs.memory_governor = None
    return vs


def test_single_shard_failure_records_exactly_once_across_dispatch_layers(tmp_path):
    config = MagicMock()
    config.embedding_provider = "voyage-ai"
    vs = _make_vs(tmp_path)
    shard_name = "code-indexer-temporal-voyage_code_3-2024Q1"

    with (
        patch(
            "code_indexer.services.temporal.temporal_fusion_dispatch"
            "._create_embedding_provider_for_collection",
            return_value=MagicMock(),
        ),
        patch(
            "code_indexer.services.temporal.temporal_search_service"
            ".TemporalSearchService.query_temporal",
            side_effect=RuntimeError("simulated provider failure"),
        ),
        patch(
            "code_indexer.services.temporal.temporal_fusion_dispatch"
            ".record_temporal_success"
        ) as mock_success,
        patch(
            "code_indexer.services.temporal.temporal_fusion_dispatch"
            ".record_temporal_failure"
        ) as mock_failure,
    ):
        _query_shards_raw(config, vs, [shard_name], "q", 30, None, None, resolver=None)

    assert mock_success.call_count == 0
    assert mock_failure.call_count == 1, (
        "a single logical shard-query failure must be recorded EXACTLY "
        f"ONCE across dispatch layers, got {mock_failure.call_count} calls: "
        f"{mock_failure.call_args_list}"
    )


def test_single_shard_success_records_exactly_once_across_dispatch_layers(tmp_path):
    from code_indexer.services.temporal.temporal_search_service import (
        TemporalSearchResults,
    )

    config = MagicMock()
    config.embedding_provider = "voyage-ai"
    vs = _make_vs(tmp_path)
    shard_name = "code-indexer-temporal-voyage_code_3-2024Q1"

    with (
        patch(
            "code_indexer.services.temporal.temporal_fusion_dispatch"
            "._create_embedding_provider_for_collection",
            return_value=MagicMock(),
        ),
        patch(
            "code_indexer.services.temporal.temporal_search_service"
            ".TemporalSearchService.query_temporal",
            return_value=TemporalSearchResults(
                results=[],
                query="q",
                filter_type="none",
                filter_value=None,
                total_found=0,
            ),
        ),
        patch(
            "code_indexer.services.temporal.temporal_fusion_dispatch"
            ".record_temporal_success"
        ) as mock_success,
        patch(
            "code_indexer.services.temporal.temporal_fusion_dispatch"
            ".record_temporal_failure"
        ) as mock_failure,
    ):
        _query_shards_raw(config, vs, [shard_name], "q", 30, None, None, resolver=None)

    assert mock_failure.call_count == 0
    assert mock_success.call_count == 1, (
        "a single logical shard-query success must be recorded EXACTLY "
        f"ONCE across dispatch layers, got {mock_success.call_count} calls"
    )
