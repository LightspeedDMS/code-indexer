"""
Tests for Story #1400 Phase 4 additions to execute_temporal_query_with_fusion:

Per the FINAL LOCKED DESIGN's adjudicated resolution (Codex's locus adopted):
cumulative fusion + chronological resort happens INSIDE
temporal_fusion_dispatch.py's shard loop (_query_shards_raw), not in a
separate worker -- the worker (deferred to a later phase, not built here)
would be a thin consumer of already-correct data.

Covers:
- on_shards_discovered(total) fires once, with the POST-health-filter count.
- on_shard_complete(shards_attempted, shards_succeeded, cumulative_results)
  fires once per ATTEMPTED shard (success OR swallowed exception), with
  already-fused + chrono-resorted results.
- TemporalSearchResults carries shards_total/shards_attempted/shards_succeeded.
- cancel_check() returning True raises InterruptedError before the next
  shard is queried (cooperative cancellation, CRITICAL 2).

Mirrors the exact patching pattern of test_temporal_fusion_dispatch.py.
"""

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.config import VoyageAIConfig
from code_indexer.services.temporal.temporal_fusion_dispatch import (
    execute_temporal_query_with_fusion,
)
from code_indexer.services.temporal.temporal_search_service import (
    TemporalSearchResult,
    TemporalSearchResults,
)


def _make_result(
    file_path: str = "foo.py", score: float = 0.9, ts: int = 1000
) -> TemporalSearchResult:
    return TemporalSearchResult(
        file_path=file_path,
        chunk_index=0,
        content="content",
        score=score,
        metadata={},
        temporal_context={"commit_hash": "abc123", "commit_timestamp": ts},
    )


def _make_results_with(results, query: str = "test") -> TemporalSearchResults:
    return TemporalSearchResults(
        results=results,
        query=query,
        filter_type="none",
        filter_value=None,
        total_found=len(results),
    )


def _make_mock_config():
    config = MagicMock()
    config.embedding_provider = "voyage-ai"
    config.voyage_ai = VoyageAIConfig(model="voyage-code-3")
    config.temporal.embedders = ["voyage-code-3"]
    config.temporal.active_embedder = "voyage-code-3"
    return config


def _make_mock_vector_store(project_root: Path):
    vs = MagicMock()
    vs.project_root = project_root
    return vs


def _two_shard_provider():
    return [
        (
            "code-indexer-temporal-voyage_code_3",
            ["shard_2026_q1", "shard_2026_q2"],
        )
    ]


class TestOnShardsDiscoveredCallback:
    def test_fires_once_with_post_health_filter_count(self, tmp_path):
        config = _make_mock_config()
        vector_store = _make_mock_vector_store(tmp_path)
        discovered_calls = []

        with (
            patch(
                "code_indexer.services.temporal.temporal_fusion_dispatch._discover_provider_shards_with_pruning",
                return_value=_two_shard_provider(),
            ),
            patch(
                "code_indexer.services.temporal.temporal_fusion_dispatch.filter_healthy_temporal_providers",
                side_effect=lambda cols: (cols, []),
            ),
            patch(
                "code_indexer.services.temporal.temporal_migration.migrate_legacy_temporal_collection",
            ),
            patch(
                "code_indexer.services.temporal.temporal_search_service.TemporalSearchService"
            ) as MockService,
        ):
            mock_service_instance = MagicMock()
            mock_service_instance.query_temporal.side_effect = [
                _make_results_with([_make_result("a.py", ts=100)]),
                _make_results_with([_make_result("b.py", ts=200)]),
            ]
            MockService.return_value = mock_service_instance

            execute_temporal_query_with_fusion(
                config=config,
                index_path=tmp_path,
                vector_store=vector_store,
                query_text="q",
                limit=5,
                on_shards_discovered=lambda total: discovered_calls.append(total),
            )

        assert discovered_calls == [2]


class TestOnShardCompleteCallback:
    def test_fires_once_per_attempted_shard_with_cumulative_fused_results(
        self, tmp_path
    ):
        config = _make_mock_config()
        vector_store = _make_mock_vector_store(tmp_path)
        shard_complete_calls = []

        with (
            patch(
                "code_indexer.services.temporal.temporal_fusion_dispatch._discover_provider_shards_with_pruning",
                return_value=_two_shard_provider(),
            ),
            patch(
                "code_indexer.services.temporal.temporal_fusion_dispatch.filter_healthy_temporal_providers",
                side_effect=lambda cols: (cols, []),
            ),
            patch(
                "code_indexer.services.temporal.temporal_migration.migrate_legacy_temporal_collection",
            ),
            patch(
                "code_indexer.services.temporal.temporal_search_service.TemporalSearchService"
            ) as MockService,
        ):
            mock_service_instance = MagicMock()
            mock_service_instance.query_temporal.side_effect = [
                _make_results_with([_make_result("a.py", ts=100)]),
                _make_results_with([_make_result("b.py", ts=200)]),
            ]
            MockService.return_value = mock_service_instance

            execute_temporal_query_with_fusion(
                config=config,
                index_path=tmp_path,
                vector_store=vector_store,
                query_text="q",
                limit=5,
                on_shard_complete=lambda attempted, succeeded, cumulative: (
                    shard_complete_calls.append((attempted, succeeded, len(cumulative)))
                ),
            )

        assert shard_complete_calls == [(1, 1, 1), (2, 2, 2)]

    def test_fires_for_attempted_shard_even_on_swallowed_exception(self, tmp_path):
        config = _make_mock_config()
        vector_store = _make_mock_vector_store(tmp_path)
        shard_complete_calls = []

        with (
            patch(
                "code_indexer.services.temporal.temporal_fusion_dispatch._discover_provider_shards_with_pruning",
                return_value=_two_shard_provider(),
            ),
            patch(
                "code_indexer.services.temporal.temporal_fusion_dispatch.filter_healthy_temporal_providers",
                side_effect=lambda cols: (cols, []),
            ),
            patch(
                "code_indexer.services.temporal.temporal_migration.migrate_legacy_temporal_collection",
            ),
            patch(
                "code_indexer.services.temporal.temporal_search_service.TemporalSearchService"
            ) as MockService,
        ):
            mock_service_instance = MagicMock()
            mock_service_instance.query_temporal.side_effect = [
                Exception("shard query boom"),
                _make_results_with([_make_result("b.py", ts=200)]),
            ]
            MockService.return_value = mock_service_instance

            execute_temporal_query_with_fusion(
                config=config,
                index_path=tmp_path,
                vector_store=vector_store,
                query_text="q",
                limit=5,
                on_shard_complete=lambda attempted, succeeded, cumulative: (
                    shard_complete_calls.append((attempted, succeeded, len(cumulative)))
                ),
            )

        # First shard attempted but failed (not succeeded); second succeeded.
        assert shard_complete_calls == [(1, 0, 0), (2, 1, 1)]


class TestShardCounters:
    def test_result_carries_shard_counters(self, tmp_path):
        config = _make_mock_config()
        vector_store = _make_mock_vector_store(tmp_path)

        with (
            patch(
                "code_indexer.services.temporal.temporal_fusion_dispatch._discover_provider_shards_with_pruning",
                return_value=_two_shard_provider(),
            ),
            patch(
                "code_indexer.services.temporal.temporal_fusion_dispatch.filter_healthy_temporal_providers",
                side_effect=lambda cols: (cols, []),
            ),
            patch(
                "code_indexer.services.temporal.temporal_migration.migrate_legacy_temporal_collection",
            ),
            patch(
                "code_indexer.services.temporal.temporal_search_service.TemporalSearchService"
            ) as MockService,
        ):
            mock_service_instance = MagicMock()
            mock_service_instance.query_temporal.side_effect = [
                Exception("boom"),
                _make_results_with([_make_result("b.py", ts=200)]),
            ]
            MockService.return_value = mock_service_instance

            result = execute_temporal_query_with_fusion(
                config=config,
                index_path=tmp_path,
                vector_store=vector_store,
                query_text="q",
                limit=5,
            )

        assert result.shards_total == 2
        assert result.shards_attempted == 2
        assert result.shards_succeeded == 1


class TestCancelCheckCooperativeCancellation:
    def test_cancel_check_true_raises_interrupted_error_before_next_shard(
        self, tmp_path
    ):
        config = _make_mock_config()
        vector_store = _make_mock_vector_store(tmp_path)

        with (
            patch(
                "code_indexer.services.temporal.temporal_fusion_dispatch._discover_provider_shards_with_pruning",
                return_value=_two_shard_provider(),
            ),
            patch(
                "code_indexer.services.temporal.temporal_fusion_dispatch.filter_healthy_temporal_providers",
                side_effect=lambda cols: (cols, []),
            ),
            patch(
                "code_indexer.services.temporal.temporal_migration.migrate_legacy_temporal_collection",
            ),
            patch(
                "code_indexer.services.temporal.temporal_search_service.TemporalSearchService"
            ) as MockService,
        ):
            mock_service_instance = MagicMock()
            mock_service_instance.query_temporal.side_effect = [
                _make_results_with([_make_result("a.py", ts=100)]),
                _make_results_with([_make_result("b.py", ts=200)]),
            ]
            MockService.return_value = mock_service_instance

            # cancel_check returns False for the first shard, True before the second.
            calls = {"n": 0}

            def cancel_check():
                calls["n"] += 1
                return calls["n"] > 1

            with pytest.raises(InterruptedError):
                execute_temporal_query_with_fusion(
                    config=config,
                    index_path=tmp_path,
                    vector_store=vector_store,
                    query_text="q",
                    limit=5,
                    cancel_check=cancel_check,
                )

            # Only the first shard's query_temporal call should have happened.
            assert mock_service_instance.query_temporal.call_count == 1

    def test_no_cancel_check_behaves_unchanged(self, tmp_path):
        """Backward compatibility: omitting cancel_check must not raise."""
        config = _make_mock_config()
        vector_store = _make_mock_vector_store(tmp_path)

        with (
            patch(
                "code_indexer.services.temporal.temporal_fusion_dispatch._discover_provider_shards_with_pruning",
                return_value=_two_shard_provider(),
            ),
            patch(
                "code_indexer.services.temporal.temporal_fusion_dispatch.filter_healthy_temporal_providers",
                side_effect=lambda cols: (cols, []),
            ),
            patch(
                "code_indexer.services.temporal.temporal_migration.migrate_legacy_temporal_collection",
            ),
            patch(
                "code_indexer.services.temporal.temporal_search_service.TemporalSearchService"
            ) as MockService,
        ):
            mock_service_instance = MagicMock()
            mock_service_instance.query_temporal.side_effect = [
                _make_results_with([_make_result("a.py", ts=100)]),
                _make_results_with([_make_result("b.py", ts=200)]),
            ]
            MockService.return_value = mock_service_instance

            result = execute_temporal_query_with_fusion(
                config=config,
                index_path=tmp_path,
                vector_store=vector_store,
                query_text="q",
                limit=5,
            )
        assert len(result.results) == 2


class TestMaybeInjectInternalLatencyLever:
    """Story #1400 Phase 10: optional injectable latency hook, called once
    per shard attempt with "temporal-shard" as the target. CLI/solo/daemon
    call sites never pass one -- None (default) means byte-identical
    behavior (already proven by every OTHER test in this file, none of
    which pass this param)."""

    def test_called_once_per_shard_attempt_with_temporal_shard_target(self, tmp_path):
        config = _make_mock_config()
        vector_store = _make_mock_vector_store(tmp_path)
        injector_calls = []

        with (
            patch(
                "code_indexer.services.temporal.temporal_fusion_dispatch._discover_provider_shards_with_pruning",
                return_value=_two_shard_provider(),
            ),
            patch(
                "code_indexer.services.temporal.temporal_fusion_dispatch.filter_healthy_temporal_providers",
                side_effect=lambda cols: (cols, []),
            ),
            patch(
                "code_indexer.services.temporal.temporal_migration.migrate_legacy_temporal_collection",
            ),
            patch(
                "code_indexer.services.temporal.temporal_search_service.TemporalSearchService"
            ) as MockService,
        ):
            mock_service_instance = MagicMock()
            mock_service_instance.query_temporal.side_effect = [
                _make_results_with([_make_result("a.py", ts=100)]),
                _make_results_with([_make_result("b.py", ts=200)]),
            ]
            MockService.return_value = mock_service_instance

            execute_temporal_query_with_fusion(
                config=config,
                index_path=tmp_path,
                vector_store=vector_store,
                query_text="q",
                limit=5,
                maybe_inject_internal_latency=lambda target: injector_calls.append(
                    target
                ),
            )

        assert injector_calls == ["temporal-shard", "temporal-shard"]


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
