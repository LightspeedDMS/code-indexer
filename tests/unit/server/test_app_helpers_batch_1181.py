"""Tests for _apply_rest_semantic_truncation and _apply_rest_fts_truncation
using store_batch instead of per-result store calls (Bug #1181).

Verifies:
- store_batch called ONCE (not store N times) for N large results
- Each large result gets distinct handle + correct preview/has_more/total_size
- Small results are left unchanged (no store_batch call)
- Fail-open: batch error leaves results with cache_handle=None, has_more=False
- Mixed large/small: only large results get handles; two handles are distinct

TDD: tests written BEFORE implementation.
"""

import pytest


@pytest.fixture
def cache_100(tmp_path):
    """PayloadCache with 100-char preview backed by real SQLite."""
    from code_indexer.server.cache.payload_cache import (
        PayloadCache,
        PayloadCacheConfig,
    )

    config = PayloadCacheConfig(preview_size_chars=100, cache_ttl_seconds=300)
    cache = PayloadCache(db_path=tmp_path / "cache.db", config=config)
    cache.initialize()
    yield cache
    cache.close()


def _patch_cache(cache):
    """Return (store_calls, store_batch_calls) counters and patch cache in place."""
    store_calls = [0]
    store_batch_calls = [0]
    original_store = cache.store
    original_store_batch = cache.store_batch

    def patched_store(content):
        store_calls[0] += 1
        return original_store(content)

    def patched_store_batch(contents):
        store_batch_calls[0] += 1
        return original_store_batch(contents)

    cache.store = patched_store
    cache.store_batch = patched_store_batch
    return store_calls, store_batch_calls


class TestApplyRestSemanticTruncationBatch:
    """_apply_rest_semantic_truncation must batch store calls (Bug #1181)."""

    def test_calls_store_batch_once_not_store_n_times(self, cache_100):
        """With 3 large results, store_batch must be called ONCE, not store 3 times."""
        from code_indexer.server.app_helpers import _apply_rest_semantic_truncation

        store_calls, store_batch_calls = _patch_cache(cache_100)

        results = [
            {"code_snippet": "X" * 200, "file_path": f"f{i}.py"} for i in range(3)
        ]
        _apply_rest_semantic_truncation(results, cache_100)

        assert store_batch_calls[0] == 1, (
            f"Expected store_batch called once, got {store_batch_calls[0]}"
        )
        assert store_calls[0] == 0, f"Expected store NOT called, got {store_calls[0]}"

    def test_each_large_result_gets_distinct_handle(self, cache_100):
        """Each large result must get a distinct cache_handle."""
        from code_indexer.server.app_helpers import _apply_rest_semantic_truncation

        results = [
            {"code_snippet": f"{'A' * 200}_{i}", "file_path": f"f{i}.py"}
            for i in range(3)
        ]
        out = _apply_rest_semantic_truncation(results, cache_100)

        handles = [r["cache_handle"] for r in out]
        assert len(set(handles)) == 3, f"Expected 3 distinct handles, got {handles}"

    def test_correct_preview_and_metadata_per_result(self, cache_100):
        """Each large result must have preview=content[:100], has_more=True, total_size."""
        from code_indexer.server.app_helpers import _apply_rest_semantic_truncation

        content = "B" * 200
        results = [{"code_snippet": content, "file_path": "test.py"}]
        out = _apply_rest_semantic_truncation(results, cache_100)

        r = out[0]
        assert r["preview"] == "B" * 100
        assert r["has_more"] is True
        assert r["total_size"] == 200
        assert r["cache_handle"] is not None
        assert "code_snippet" not in r

    def test_small_results_leave_no_store_batch_call(self, cache_100):
        """Small results (< preview_size) must NOT trigger store_batch."""
        from code_indexer.server.app_helpers import _apply_rest_semantic_truncation

        _, store_batch_calls = _patch_cache(cache_100)

        results = [{"code_snippet": "small", "file_path": "f.py"}]
        out = _apply_rest_semantic_truncation(results, cache_100)

        assert store_batch_calls[0] == 0, (
            "store_batch must NOT be called for all-small results"
        )
        assert out[0]["cache_handle"] is None
        assert out[0]["has_more"] is False

    def test_fail_open_on_batch_error(self, cache_100):
        """If store_batch raises, results must have cache_handle=None, has_more=False."""
        from code_indexer.server.app_helpers import _apply_rest_semantic_truncation

        def failing_store_batch(contents):
            raise RuntimeError("Simulated batch failure")

        cache_100.store_batch = failing_store_batch

        results = [{"code_snippet": "X" * 200, "file_path": "f.py"}]
        # Must not raise
        out = _apply_rest_semantic_truncation(results, cache_100)

        assert out[0]["cache_handle"] is None
        assert out[0]["has_more"] is False

    def test_mixed_large_and_small_results(self, cache_100):
        """Mix: only large results get handles; distinct handles across large results."""
        from code_indexer.server.app_helpers import _apply_rest_semantic_truncation

        results = [
            {"code_snippet": "X" * 200, "file_path": "large.py"},
            {"code_snippet": "tiny", "file_path": "small.py"},
            {"code_snippet": "Y" * 200, "file_path": "large2.py"},
        ]
        out = _apply_rest_semantic_truncation(results, cache_100)

        assert out[0]["has_more"] is True
        assert out[0]["cache_handle"] is not None

        assert out[1]["has_more"] is False
        assert out[1]["cache_handle"] is None

        assert out[2]["has_more"] is True
        assert out[2]["cache_handle"] is not None

        assert out[0]["cache_handle"] != out[2]["cache_handle"]


class TestApplyRestFtsTruncationBatch:
    """_apply_rest_fts_truncation must batch store calls (Bug #1181)."""

    def test_calls_store_batch_once_not_store_n_times(self, cache_100):
        """With 3 large snippet results, store_batch must be called ONCE."""
        from code_indexer.server.app_helpers import _apply_rest_fts_truncation

        store_calls, store_batch_calls = _patch_cache(cache_100)

        results = [{"snippet": "X" * 200, "file_path": f"f{i}.py"} for i in range(3)]
        _apply_rest_fts_truncation(results, cache_100)

        assert store_batch_calls[0] == 1, (
            f"Expected store_batch called once, got {store_batch_calls[0]}"
        )
        assert store_calls[0] == 0, f"Expected store NOT called, got {store_calls[0]}"

    def test_snippet_correct_fields_after_truncation(self, cache_100):
        """FTS snippet truncation must set snippet_preview, snippet_cache_handle, etc."""
        from code_indexer.server.app_helpers import _apply_rest_fts_truncation

        results = [{"snippet": "S" * 200, "file_path": "f.py"}]
        out = _apply_rest_fts_truncation(results, cache_100)

        r = out[0]
        assert r["snippet_has_more"] is True
        assert r["snippet_preview"] == "S" * 100
        assert r["snippet_cache_handle"] is not None
        assert r["snippet_total_size"] == 200
        assert "snippet" not in r

    def test_fail_open_on_batch_error(self, cache_100):
        """If store_batch raises, FTS results must have snippet_cache_handle=None."""
        from code_indexer.server.app_helpers import _apply_rest_fts_truncation

        def failing_store_batch(contents):
            raise RuntimeError("Batch failure")

        cache_100.store_batch = failing_store_batch

        results = [{"snippet": "X" * 200, "file_path": "f.py"}]
        out = _apply_rest_fts_truncation(results, cache_100)

        assert out[0]["snippet_cache_handle"] is None
        assert out[0]["snippet_has_more"] is False

    def test_small_snippet_leaves_no_store_batch_call(self, cache_100):
        """Small FTS snippets must NOT trigger store_batch."""
        from code_indexer.server.app_helpers import _apply_rest_fts_truncation

        _, store_batch_calls = _patch_cache(cache_100)

        results = [{"snippet": "tiny", "file_path": "f.py"}]
        _apply_rest_fts_truncation(results, cache_100)

        assert store_batch_calls[0] == 0, (
            "store_batch must NOT be called for small snippets"
        )
