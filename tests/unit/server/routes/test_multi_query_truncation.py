"""Unit tests for Multi-Query REST API Truncation (Story #683 - Bug 2).

BUG: REST API /api/query/multi endpoint returns results without payload truncation.
The MCP handlers apply truncation but the REST endpoint does not.

These tests verify that:
1. Multi-repo search results have truncation applied via helper function

Story #50: Converted from async to sync since underlying handlers are sync.
"""

import pytest


class TestMultiQueryTruncation:
    """Tests for REST API multi-query truncation helper."""

    @pytest.fixture
    def cache_100_chars(self, tmp_path):
        """Create PayloadCache with 100 char preview for easy testing.

        Story #50: Converted to sync fixture since PayloadCache.initialize() is sync.
        """
        from code_indexer.server.cache.payload_cache import (
            PayloadCache,
            PayloadCacheConfig,
        )

        config = PayloadCacheConfig(preview_size_chars=100, max_fetch_size_chars=200)
        cache = PayloadCache(db_path=tmp_path / "test_cache.db", config=config)
        cache.initialize()
        yield cache
        cache.close()

    def test_multi_repo_results_truncated(self, cache_100_chars):
        """Multi-repo grouped results have truncation applied to each result.

        Story #50: Converted to sync test since _apply_multi_truncation is now sync.
        """
        from code_indexer.server.routes.multi_query_routes import (
            _apply_multi_truncation,
        )
        from code_indexer.server import app as app_module

        original = getattr(app_module.app.state, "payload_cache", None)
        app_module.app.state.payload_cache = cache_100_chars

        try:
            # Multi-repo format: Dict[str, List[Dict]]
            grouped_results = {
                "repo-alpha": [
                    {
                        "file_path": "/src/main.py",
                        "content": "X" * 500,  # Large - should be truncated
                        "score": 0.92,
                    },
                ],
                "repo-beta": [
                    {
                        "file_path": "/lib/utils.py",
                        "content": "small",  # Small - not truncated
                        "score": 0.88,
                    },
                ],
            }

            # Now sync call (no await)
            truncated = _apply_multi_truncation(grouped_results, "semantic")

            # Verify repo-alpha result is truncated
            assert len(truncated["repo-alpha"]) == 1
            alpha_result = truncated["repo-alpha"][0]
            assert alpha_result["has_more"] is True
            assert alpha_result["cache_handle"] is not None
            assert alpha_result["preview"] == "X" * 100
            assert "content" not in alpha_result

            # Verify repo-beta result is NOT truncated (small content)
            assert len(truncated["repo-beta"]) == 1
            beta_result = truncated["repo-beta"][0]
            assert beta_result["has_more"] is False
            assert beta_result["cache_handle"] is None
            assert beta_result["content"] == "small"
        finally:
            if original is None:
                if hasattr(app_module.app.state, "payload_cache"):
                    delattr(app_module.app.state, "payload_cache")
            else:
                app_module.app.state.payload_cache = original
