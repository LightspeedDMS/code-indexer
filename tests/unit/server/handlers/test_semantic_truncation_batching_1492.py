"""Unit tests for Story #1492 AC2: semantic truncation uses store_batch.

Finding C4 (report rank 10): _apply_payload_truncation previously called
PayloadCache.store() once per oversized result (one INSERT+commit per
result). This mirrors the already-correct _apply_fts_payload_truncation
implementation: exactly ONE payload_cache.store_batch() call per pass,
handles returned in the same order as the oversized results, content
identical to the prior per-result truncate_result() path.

Real SQLite-backed PayloadCache (no mocking of the cache itself, per
project Anti-Mock rule) -- we spy on the bound store()/store_batch()
methods (wrapping, not replacing, the real implementation) purely to count
invocations.
"""

import uuid

# cache_100_chars fixture: preview_size_chars=100, max_fetch_size_chars=200
PREVIEW_SIZE_CHARS = 100
MAX_FETCH_SIZE_CHARS = 200

CONTENT_A_SIZE = 500
CONTENT_B_SIZE = 600
CONTENT_C_SIZE = 700
CONTENT_LARGE_SIZE = 900
CONTENT_MIXED_LARGE_SIZE = 500

EXPECTED_ONE_BATCH_CALL = 1
EXPECTED_ZERO_BATCH_CALLS = 0
EXPECTED_ZERO_STORE_CALLS = 0
EXPECTED_THREE_DISTINCT_HANDLES = 3
UUID_VERSION = 4
FIRST_PAGE = 0


def _install_cache(monkeypatch, cache):
    from code_indexer.server import app as app_module

    monkeypatch.setattr(app_module.app.state, "payload_cache", cache, raising=False)
    return cache


def _spy_counts(monkeypatch, cache):
    """Wrap cache.store / cache.store_batch with call counters, still
    delegating to the real implementation (a spy, not a mock replacement).
    """
    counts = {"store": 0, "store_batch": 0}

    real_store = cache.store
    real_store_batch = cache.store_batch

    def counting_store(content):
        counts["store"] += 1
        return real_store(content)

    def counting_store_batch(contents):
        counts["store_batch"] += 1
        return real_store_batch(contents)

    monkeypatch.setattr(cache, "store", counting_store)
    monkeypatch.setattr(cache, "store_batch", counting_store_batch)
    return counts


def _assert_is_uuid4(value: str) -> None:
    # uuid.UUID(value, version=4) would silently NORMALIZE a parsed UUID's
    # version bits rather than reject a non-v4 UUID -- assert the parsed
    # value's actual .version field instead.
    assert uuid.UUID(value).version == UUID_VERSION


class TestSemanticTruncationBatchingCount:
    """AC2: exactly one store_batch() transaction per truncation pass."""

    def test_multiple_oversized_results_use_one_store_batch_call(
        self, monkeypatch, cache_100_chars
    ):
        from code_indexer.server.mcp.handlers import _apply_payload_truncation

        _install_cache(monkeypatch, cache_100_chars)
        counts = _spy_counts(monkeypatch, cache_100_chars)

        results = [
            {"content": "A" * CONTENT_A_SIZE, "file_path": "/a.py"},
            {"content": "B" * CONTENT_B_SIZE, "file_path": "/b.py"},
            {"code_snippet": "C" * CONTENT_C_SIZE, "file_path": "/c.py"},
        ]

        truncated = _apply_payload_truncation(results)

        # Exactly ONE batched transaction for all N oversized results --
        # never a per-result store() call.
        assert counts["store_batch"] == EXPECTED_ONE_BATCH_CALL
        assert counts["store"] == EXPECTED_ZERO_STORE_CALLS

        assert truncated[0]["preview"] == "A" * PREVIEW_SIZE_CHARS
        assert truncated[0]["total_size"] == CONTENT_A_SIZE
        assert truncated[0]["has_more"] is True
        assert "content" not in truncated[0]
        _assert_is_uuid4(truncated[0]["cache_handle"])

        assert truncated[1]["preview"] == "B" * PREVIEW_SIZE_CHARS
        assert truncated[1]["total_size"] == CONTENT_B_SIZE
        _assert_is_uuid4(truncated[1]["cache_handle"])

        assert truncated[2]["preview"] == "C" * PREVIEW_SIZE_CHARS
        assert truncated[2]["total_size"] == CONTENT_C_SIZE
        assert "code_snippet" not in truncated[2]
        _assert_is_uuid4(truncated[2]["cache_handle"])

        # Handles are distinct, in the same order as the results, and each
        # one retrieves the CORRECT (not swapped) content.
        handles = [r["cache_handle"] for r in truncated]
        assert len(set(handles)) == EXPECTED_THREE_DISTINCT_HANDLES

        assert cache_100_chars.retrieve(handles[0]).content == (
            "A" * MAX_FETCH_SIZE_CHARS
        )
        assert cache_100_chars.retrieve(handles[1]).content == (
            "B" * MAX_FETCH_SIZE_CHARS
        )
        assert cache_100_chars.retrieve(handles[2]).content == (
            "C" * MAX_FETCH_SIZE_CHARS
        )

    def test_no_oversized_results_never_calls_store_batch(
        self, monkeypatch, cache_100_chars
    ):
        from code_indexer.server.mcp.handlers import _apply_payload_truncation

        _install_cache(monkeypatch, cache_100_chars)
        counts = _spy_counts(monkeypatch, cache_100_chars)

        results = [{"content": "small"}]
        truncated = _apply_payload_truncation(results)

        assert counts["store_batch"] == EXPECTED_ZERO_BATCH_CALLS
        assert counts["store"] == EXPECTED_ZERO_STORE_CALLS
        assert truncated[0]["content"] == "small"
        assert truncated[0]["cache_handle"] is None
        assert truncated[0]["has_more"] is False


class TestSemanticTruncationBatchingContent:
    """AC2: batched content is retrievable and behaves identically."""

    def test_handles_are_immediately_retrievable(self, monkeypatch, cache_100_chars):
        """A store_batch()-issued handle must be immediately retrievable --
        matching the CLAUDE.md invariant that store_batch rows are visible
        cross-node right away (solo SQLite path here; PG behavior is
        covered by the payload_cache backend's own test suite)."""
        from code_indexer.server.mcp.handlers import _apply_payload_truncation

        _install_cache(monkeypatch, cache_100_chars)

        results = [{"content": "Z" * CONTENT_LARGE_SIZE}]
        truncated = _apply_payload_truncation(results)
        handle = truncated[0]["cache_handle"]

        result = cache_100_chars.retrieve(handle, page=FIRST_PAGE)
        assert result.content == "Z" * MAX_FETCH_SIZE_CHARS

    def test_mixed_small_and_large_results(self, monkeypatch, cache_100_chars):
        from code_indexer.server.mcp.handlers import _apply_payload_truncation

        _install_cache(monkeypatch, cache_100_chars)
        counts = _spy_counts(monkeypatch, cache_100_chars)

        results = [
            {"content": "small"},
            {"content": "L" * CONTENT_MIXED_LARGE_SIZE},
            {"content": "tiny"},
        ]
        truncated = _apply_payload_truncation(results)

        assert counts["store_batch"] == EXPECTED_ONE_BATCH_CALL
        assert truncated[0]["content"] == "small"
        assert truncated[0]["cache_handle"] is None
        assert truncated[1]["has_more"] is True
        assert truncated[1]["total_size"] == CONTENT_MIXED_LARGE_SIZE
        assert truncated[2]["content"] == "tiny"
        assert truncated[2]["cache_handle"] is None
