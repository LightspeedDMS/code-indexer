"""Tests for graceful degradation in _apply_cli_rerank_and_filter.

Covers: no API keys (disabled path), both providers sinbinned, both HTTP failures.
Story #693 -- Epic #689.
"""

from code_indexer.services.provider_health_monitor import ProviderHealthMonitor

from .conftest import ServiceStack, make_semantic


def _apply():
    from code_indexer.cli_search_funnel import _apply_cli_rerank_and_filter

    return _apply_cli_rerank_and_filter


class TestGracefulDegradation:
    def test_no_api_keys_does_not_raise_and_truncates(
        self,
        real_monitor: ProviderHealthMonitor,
        no_key_config,
    ) -> None:
        """Empty model strings -> disabled path in _apply_reranking_sync -> truncated."""
        apply = _apply()
        results = [make_semantic(score=float(i)) for i in range(6)]
        out = apply(
            results=results,
            rerank_query="authentication logic",
            rerank_instruction=None,
            config=no_key_config,
            user_limit=4,
            health_monitor=real_monitor,
        )
        assert len(out) == 4

    def test_both_providers_sinbinned_returns_truncated_original_order(
        self,
        wire_both_key_stack: ServiceStack,
    ) -> None:
        wire_both_key_stack.monitor.sinbin("voyage-reranker")
        wire_both_key_stack.monitor.sinbin("cohere-reranker")
        apply = _apply()
        results = [make_semantic(score=float(i)) for i in range(5)]
        out = apply(
            results=results,
            rerank_query="auth",
            rerank_instruction=None,
            config=wire_both_key_stack.config,
            user_limit=3,
            health_monitor=wire_both_key_stack.monitor,
        )
        assert len(out) == 3
        for i, r in enumerate(out):
            assert r["score"] == results[i]["score"]

    def test_both_http_failures_fall_back_gracefully(
        self,
        both_rerankers_failing_patched: ServiceStack,
    ) -> None:
        apply = _apply()
        results = [make_semantic(score=float(i)) for i in range(5)]
        out = apply(
            results=results,
            rerank_query="auth",
            rerank_instruction=None,
            config=both_rerankers_failing_patched.config,
            user_limit=3,
            health_monitor=both_rerankers_failing_patched.monitor,
        )
        assert len(out) == 3
