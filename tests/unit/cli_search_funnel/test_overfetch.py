"""Tests for calculate_cli_overfetch_limit and no-op rerank path.

Story #693 -- Epic #689.
"""

from code_indexer.services.cli_rerank_config_shim import CliRerankConfigService
from code_indexer.services.provider_health_monitor import ProviderHealthMonitor
from code_indexer.server.mcp.reranking import MAX_CANDIDATE_LIMIT

from .conftest import make_semantic, make_fts, make_global_config


def _import_funnel():
    from code_indexer.cli_search_funnel import (
        _apply_cli_rerank_and_filter,
        calculate_cli_overfetch_limit,
    )

    return _apply_cli_rerank_and_filter, calculate_cli_overfetch_limit


# ---------------------------------------------------------------------------
# calculate_cli_overfetch_limit
# ---------------------------------------------------------------------------


class TestCalculateCliOverfetchLimit:
    def test_returns_user_limit_times_multiplier(
        self, no_key_config: CliRerankConfigService
    ) -> None:
        _, calculate = _import_funnel()
        assert calculate(user_limit=10, config=no_key_config) == 30

    def test_caps_at_max_candidate_limit(self) -> None:
        _, calculate = _import_funnel()
        cfg = CliRerankConfigService(make_global_config(overfetch=5))
        assert calculate(user_limit=100, config=cfg) == MAX_CANDIDATE_LIMIT

    def test_multiplier_one_returns_user_limit(self) -> None:
        _, calculate = _import_funnel()
        cfg = CliRerankConfigService(make_global_config(overfetch=1))
        assert calculate(user_limit=7, config=cfg) == 7

    def test_zero_limit_returns_zero(
        self, no_key_config: CliRerankConfigService
    ) -> None:
        _, calculate = _import_funnel()
        assert calculate(user_limit=0, config=no_key_config) == 0


# ---------------------------------------------------------------------------
# No-op rerank: None / empty rerank_query
# ---------------------------------------------------------------------------


class TestNoOpRerank:
    def test_none_query_truncates_to_user_limit(
        self,
        real_monitor: ProviderHealthMonitor,
        no_key_config: CliRerankConfigService,
    ) -> None:
        apply, _ = _import_funnel()
        results = [make_semantic(score=float(i)) for i in range(5)]
        out = apply(
            results=results,
            rerank_query=None,
            rerank_instruction=None,
            config=no_key_config,
            user_limit=3,
            health_monitor=real_monitor,
        )
        assert out == results[:3]

    def test_empty_string_query_truncates_to_user_limit(
        self,
        real_monitor: ProviderHealthMonitor,
        no_key_config: CliRerankConfigService,
    ) -> None:
        apply, _ = _import_funnel()
        results = [make_semantic(score=float(i)) for i in range(5)]
        out = apply(
            results=results,
            rerank_query="",
            rerank_instruction=None,
            config=no_key_config,
            user_limit=2,
            health_monitor=real_monitor,
        )
        assert out == results[:2]

    def test_empty_results_returns_empty(
        self,
        real_monitor: ProviderHealthMonitor,
        no_key_config: CliRerankConfigService,
    ) -> None:
        apply, _ = _import_funnel()
        out = apply(
            results=[],
            rerank_query=None,
            rerank_instruction=None,
            config=no_key_config,
            user_limit=10,
            health_monitor=real_monitor,
        )
        assert out == []

    def test_fts_shape_fields_preserved_on_no_op(
        self,
        real_monitor: ProviderHealthMonitor,
        no_key_config: CliRerankConfigService,
    ) -> None:
        apply, _ = _import_funnel()
        results = [
            make_fts(path=f"src/{i}.py", match_text=f"term{i}") for i in range(4)
        ]
        out = apply(
            results=results,
            rerank_query=None,
            rerank_instruction=None,
            config=no_key_config,
            user_limit=3,
            health_monitor=real_monitor,
        )
        assert len(out) == 3
        assert out[0]["match_text"] == results[0]["match_text"]
        assert out[0]["snippet"] == results[0]["snippet"]
