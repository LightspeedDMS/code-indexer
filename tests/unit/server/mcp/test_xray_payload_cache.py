"""Unit tests for _truncate_xray_result helper in xray MCP handler.

Tests that the helper correctly applies PayloadCache truncation to large
matches[] / evaluation_errors[] payloads in X-Ray job results.

Mocking strategy:
- PayloadCache: real dataclass-style fake (not a Mock) to exercise the
  actual truncation path without needing a live SQLite database.
- _utils.app_module.app.state: patched via unittest.mock.patch to inject
  the fake cache or None.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List
from unittest.mock import MagicMock, patch

import pytest


# ---------------------------------------------------------------------------
# Minimal fake PayloadCache — avoids SQLite, exercises real truncation logic
# ---------------------------------------------------------------------------


class _FakePayloadCacheConfig:
    preview_size_chars: int = 200
    # Large enough that every fixture in this file fits in a SINGLE cache
    # page (multi-page pagination is exhaustively covered by
    # test_xray_truncation_byte_budget_1928.py's real-SQLite-cache tests).
    max_fetch_size_chars: int = 1_000_000


class _FakePayloadCache:
    """Minimal fake PayloadCache with the same truncate_result contract."""

    def __init__(
        self, preview_size_chars: int = 200, max_fetch_size_chars: int = 1_000_000
    ) -> None:
        self.config = _FakePayloadCacheConfig()
        self.config.preview_size_chars = preview_size_chars
        self.config.max_fetch_size_chars = max_fetch_size_chars
        self._stored: Dict[str, str] = {}
        self._counter = 0

    def store(self, content: str) -> str:
        self._counter += 1
        handle = f"fake-handle-{self._counter}"
        self._stored[handle] = content
        return handle

    def truncate_result(self, content: str) -> dict:
        """Mirror of PayloadCache.truncate_result() logic."""
        preview_size = self.config.preview_size_chars
        if len(content) > preview_size:
            cache_handle = self.store(content)
            return {
                "preview": content[:preview_size],
                "cache_handle": cache_handle,
                "has_more": True,
                "total_size": len(content),
            }
        else:
            return {
                "content": content,
                "cache_handle": None,
                "has_more": False,
            }


# ---------------------------------------------------------------------------
# Helper builders
# ---------------------------------------------------------------------------


def _make_match(file_path: str, snippet: str = "x" * 50) -> Dict[str, Any]:
    return {
        "file_path": file_path,
        "line_number": 1,
        "code_snippet": snippet,
        "language": "python",
        "evaluator_decision": True,
    }


def _make_error(file_path: str) -> Dict[str, Any]:
    return {
        "file_path": file_path,
        "line_number": None,
        "error_type": "AttributeError",
        "error_message": "node has no attribute 'x'",
    }


def _make_small_result() -> Dict[str, Any]:
    """Result whose matches+errors JSON is under 200 chars."""
    return {
        "matches": [_make_match("a.py", "x")],
        "evaluation_errors": [],
        "files_processed": 1,
        "files_total": 1,
        "elapsed_seconds": 0.1,
    }


def _make_large_result(n_matches: int = 20) -> Dict[str, Any]:
    """Result whose matches+errors JSON exceeds 200 chars."""
    return {
        "matches": [_make_match(f"file_{i}.py", "x" * 80) for i in range(n_matches)],
        "evaluation_errors": [_make_error(f"err_{i}.py") for i in range(5)],
        "files_processed": n_matches,
        "files_total": n_matches,
        "elapsed_seconds": 1.5,
    }


def _import_helper():
    from code_indexer.server.mcp.handlers.xray import _truncate_xray_result

    return _truncate_xray_result


_LARGE_CACHE_TTL_SECONDS = 900
_LARGE_CACHE_CLEANUP_INTERVAL_SECONDS = 60


def _make_real_cache(db_path, max_fetch_size_chars: int):
    """A REAL, on-disk PayloadCache. Bug #1928 rework: the shared
    xray_truncation.truncate_result_fields() calls PayloadCache.store_batch()
    (Bug #1181 one-transaction-per-batch pattern, one row per page) plus
    store() for a small pages-v1 manifest row -- _FakePayloadCache (which
    only implements store()/truncate_result()) no longer exercises the
    real code path, so TestTruncateXrayResultLarge* uses a real cache like
    the rest of the Bug #1928 suite
    (tests/unit/server/mcp/test_xray_truncation_*_1928.py). There is now a
    SINGLE budget (max_fetch_size_chars) -- the old preview_size_chars/
    max_fetch_size_chars split is gone."""
    from code_indexer.server.cache.payload_cache import (
        PayloadCache,
        PayloadCacheConfig,
    )

    config = PayloadCacheConfig(
        preview_size_chars=max_fetch_size_chars,
        max_fetch_size_chars=max_fetch_size_chars,
        cache_ttl_seconds=_LARGE_CACHE_TTL_SECONDS,
        cleanup_interval_seconds=_LARGE_CACHE_CLEANUP_INTERVAL_SECONDS,
    )
    cache = PayloadCache(db_path=db_path, config=config)
    cache.initialize()
    return cache


@pytest.fixture
def large_cache_factory(tmp_path):
    """Factory fixture: build a REAL PayloadCache + MagicMock app (whose
    app.state.payload_cache is that real cache) for a given
    max_fetch_size_chars, guaranteeing cache.close() teardown for every
    cache built -- shared by all TestTruncateXrayResultLarge* classes to
    avoid repeating the cache/mock/patch/cleanup boilerplate per test."""
    created: List[Any] = []

    def _make(max_fetch_size_chars: int):
        db_path = tmp_path / f"payload_cache_{len(created)}.db"
        cache = _make_real_cache(db_path, max_fetch_size_chars)
        created.append(cache)
        mock_state = MagicMock()
        mock_state.payload_cache = cache
        mock_app = MagicMock()
        mock_app.state = mock_state
        return cache, mock_app

    yield _make

    for cache in created:
        cache.close()


# ---------------------------------------------------------------------------
# Tests: small result — inline, no cache
# ---------------------------------------------------------------------------


class TestTruncateXrayResultSmall:
    """Small results are returned inline without caching."""

    def test_small_result_returns_inline_no_cache_handle(self):
        """When combined payload is small, result is returned inline."""
        fake_cache = _FakePayloadCache(preview_size_chars=10_000)
        mock_state = MagicMock()
        mock_state.payload_cache = fake_cache
        mock_app = MagicMock()
        mock_app.state = mock_state

        with patch(
            "code_indexer.server.mcp.handlers._utils.app_module",
            **{"app": mock_app},
        ):
            helper = _import_helper()
            result = helper(_make_small_result())

        assert result.get("cache_handle") is None
        assert result.get("has_more") is False
        assert result.get("truncated") is False
        assert isinstance(result.get("matches"), list)
        assert isinstance(result.get("evaluation_errors"), list)

    def test_small_result_preserves_all_matches(self):
        """Small result keeps all matches inline — no truncation to first 3."""
        fake_cache = _FakePayloadCache(preview_size_chars=10_000)
        mock_state = MagicMock()
        mock_state.payload_cache = fake_cache
        mock_app = MagicMock()
        mock_app.state = mock_state

        result_in = _make_small_result()
        with patch(
            "code_indexer.server.mcp.handlers._utils.app_module",
            **{"app": mock_app},
        ):
            helper = _import_helper()
            result = helper(result_in)

        assert result["matches"] == result_in["matches"]
        assert result["evaluation_errors"] == result_in["evaluation_errors"]

    def test_small_result_preserves_top_level_metadata(self):
        """Small result keeps non-match top-level fields intact."""
        fake_cache = _FakePayloadCache(preview_size_chars=10_000)
        mock_state = MagicMock()
        mock_state.payload_cache = fake_cache
        mock_app = MagicMock()
        mock_app.state = mock_state

        result_in = _make_small_result()
        with patch(
            "code_indexer.server.mcp.handlers._utils.app_module",
            **{"app": mock_app},
        ):
            helper = _import_helper()
            result = helper(result_in)

        assert result["files_processed"] == result_in["files_processed"]
        assert result["files_total"] == result_in["files_total"]
        assert result["elapsed_seconds"] == result_in["elapsed_seconds"]

    def test_small_result_does_not_store_in_cache(self):
        """Small results must not create a cache entry."""
        fake_cache = _FakePayloadCache(preview_size_chars=10_000)
        mock_state = MagicMock()
        mock_state.payload_cache = fake_cache
        mock_app = MagicMock()
        mock_app.state = mock_state

        with patch(
            "code_indexer.server.mcp.handlers._utils.app_module",
            **{"app": mock_app},
        ):
            helper = _import_helper()
            helper(_make_small_result())

        assert len(fake_cache._stored) == 0


# ---------------------------------------------------------------------------
# Tests: large result — preview + cache_handle
# ---------------------------------------------------------------------------


class TestTruncateXrayResultLargeMarkers:
    """Large results set the cache_handle/has_more/truncated markers."""

    def test_large_result_returns_cache_handle(self, large_cache_factory):
        """Large combined payload produces a non-None cache_handle."""
        _cache, mock_app = large_cache_factory(200)
        with patch(
            "code_indexer.server.mcp.handlers._utils.app_module",
            **{"app": mock_app},
        ):
            helper = _import_helper()
            result = helper(_make_large_result())

        assert result.get("cache_handle") is not None
        assert isinstance(result["cache_handle"], str) and result["cache_handle"]

    def test_large_result_has_more_true(self, large_cache_factory):
        """Large result must have has_more=True."""
        _cache, mock_app = large_cache_factory(200)
        with patch(
            "code_indexer.server.mcp.handlers._utils.app_module",
            **{"app": mock_app},
        ):
            helper = _import_helper()
            result = helper(_make_large_result())

        assert result["has_more"] is True

    def test_large_result_truncated_true(self, large_cache_factory):
        """Large result must have truncated=True."""
        _cache, mock_app = large_cache_factory(200)
        with patch(
            "code_indexer.server.mcp.handlers._utils.app_module",
            **{"app": mock_app},
        ):
            helper = _import_helper()
            result = helper(_make_large_result())

        assert result["truncated"] is True


class TestTruncateXrayResultLargeDataIntegrity:
    """No-data-loss contract: total_pages present, full payload
    round-trips whole through the cache, and the removed dual preview
    field is gone."""

    def test_large_result_includes_total_pages(self, large_cache_factory):
        """Bug #1928: the removed `total_size` field is replaced by
        `total_pages` -- a positive count of the independently-fetchable
        cache pages holding the full result."""
        _cache, mock_app = large_cache_factory(200)
        large = _make_large_result()
        with patch(
            "code_indexer.server.mcp.handlers._utils.app_module",
            **{"app": mock_app},
        ):
            helper = _import_helper()
            result = helper(large)

        assert isinstance(result["total_pages"], int)
        assert result["total_pages"] >= 1

    def test_large_result_stores_full_payload_in_cache(self, large_cache_factory):
        """The cached pages round-trip (via xray_truncation.fetch_cached_page,
        concatenated in page order) to the full, original
        matches[]/evaluation_errors[] arrays -- whole and unmodified, per
        the Bug #1928 no-data-loss contract."""
        from code_indexer.server.mcp.handlers import xray_truncation as xt

        cache, mock_app = large_cache_factory(200)
        large = _make_large_result()
        with patch(
            "code_indexer.server.mcp.handlers._utils.app_module",
            **{"app": mock_app},
        ):
            helper = _import_helper()
            result = helper(large)

        cache_handle = result["cache_handle"]
        all_matches: list = []
        all_errors: list = []
        page_num = 1
        max_pages_safety = 200
        for _ in range(max_pages_safety):
            fetched = xt.fetch_cached_page(cache, cache_handle, page_num)
            parsed = json.loads(fetched["content"])
            all_matches.extend(parsed.get("matches", []))
            all_errors.extend(parsed.get("evaluation_errors", []))
            if not fetched["has_more"]:
                break
            page_num += 1
        else:
            raise AssertionError(
                f"pagination did not terminate within {max_pages_safety} pages"
            )

        assert all_matches == large["matches"]
        assert all_errors == large["evaluation_errors"]

    def test_large_result_does_not_include_preview_string(self, large_cache_factory):
        """Bug #1928: the duplicated matches_and_errors_preview field is
        removed -- one representation of inline findings, not two."""
        _cache, mock_app = large_cache_factory(200)
        with patch(
            "code_indexer.server.mcp.handlers._utils.app_module",
            **{"app": mock_app},
        ):
            helper = _import_helper()
            result = helper(_make_large_result())

        assert "matches_and_errors_preview" not in result


class TestTruncateXrayResultLargeWholeEntryPrefix:
    """Inline matches/errors are a genuine byte-budget-driven whole-entry
    prefix, never a fixed count -- and non-array metadata survives."""

    def test_large_result_matches_are_a_whole_entry_prefix_not_a_fixed_count(
        self, large_cache_factory
    ):
        """Bug #1928: inline matches are governed by the byte budget (a
        whole-entry prefix that itself fits the budget), never a fixed
        count of 3. budget=800 (rather than this file's usual 200) keeps
        each match entry (~234 chars) fitting VERBATIM, so this test
        exercises ordinary multi-entry packing -- not the
        separately-tested single-oversized-entry adaptive shrink, which
        200 would trigger even for just the first match alone."""
        budget = 800
        _cache, mock_app = large_cache_factory(budget)
        large = _make_large_result(n_matches=20)
        with patch(
            "code_indexer.server.mcp.handlers._utils.app_module",
            **{"app": mock_app},
        ):
            helper = _import_helper()
            result = helper(large)

        n_inline = len(result["matches"])
        assert 0 < n_inline < len(large["matches"])
        assert result["matches"] == large["matches"][:n_inline]
        inline_size = len(
            json.dumps(
                {
                    "matches": result["matches"],
                    "evaluation_errors": result["evaluation_errors"],
                }
            )
        )
        assert inline_size <= budget

    def test_large_result_errors_are_a_whole_entry_prefix_not_a_fixed_count(
        self, large_cache_factory
    ):
        """Same whole-entry-prefix-within-budget contract for
        evaluation_errors[], using a fixture (1 small match + 10 errors)
        sized so errors genuinely get a partial (not empty, not full)
        prefix -- with the default 20-match fixture, matches alone
        consume the whole budget before any error is ever reached, which
        would make an errors partial-fill assertion vacuous."""
        budget = 500
        _cache, mock_app = large_cache_factory(budget)
        result_in: Dict[str, Any] = {
            "matches": [_make_match("only.py", "x" * 10)],
            "evaluation_errors": [_make_error(f"err_{i}.py") for i in range(10)],
            "files_processed": 1,
            "files_total": 1,
            "elapsed_seconds": 0.1,
        }
        with patch(
            "code_indexer.server.mcp.handlers._utils.app_module",
            **{"app": mock_app},
        ):
            helper = _import_helper()
            result = helper(result_in)

        assert result["truncated"] is True, (
            "fixture premise: this result must actually need truncation"
        )
        n_inline = len(result["evaluation_errors"])
        assert 0 < n_inline < len(result_in["evaluation_errors"]), (
            "fixture must produce a genuine partial (not empty, not full) "
            "errors prefix to meaningfully exercise the budget"
        )
        assert result["evaluation_errors"] == result_in["evaluation_errors"][:n_inline]
        inline_size = len(
            json.dumps(
                {
                    "matches": result["matches"],
                    "evaluation_errors": result["evaluation_errors"],
                }
            )
        )
        assert inline_size <= budget

    def test_large_result_preserves_metadata_fields(self, large_cache_factory):
        """Large result preserves files_processed, files_total, elapsed_seconds."""
        _cache, mock_app = large_cache_factory(200)
        large = _make_large_result()
        with patch(
            "code_indexer.server.mcp.handlers._utils.app_module",
            **{"app": mock_app},
        ):
            helper = _import_helper()
            result = helper(large)

        assert result["files_processed"] == large["files_processed"]
        assert result["files_total"] == large["files_total"]
        assert result["elapsed_seconds"] == large["elapsed_seconds"]


# ---------------------------------------------------------------------------
# Tests: cache unavailable — return full result unchanged
# ---------------------------------------------------------------------------


class TestTruncateXrayResultCacheUnavailable:
    """Bug #1928 round 3 (P3, Codex): when payload_cache is unavailable,
    the result must still be BOUNDED (never returned in full) -- flagged
    cache_unavailable=True so a caller can tell "genuinely small" apart
    from "cache was down"."""

    def test_cache_unavailable_bounds_a_large_result(self):
        """No payload_cache on app.state: a large result is bounded, not
        returned in full."""
        mock_state = MagicMock(spec=[])  # no payload_cache attribute
        mock_app = MagicMock()
        mock_app.state = mock_state

        # n_matches=40 (~8500 chars serialized) reliably exceeds the
        # 5000-char default fallback budget the cache-unavailable
        # degrade path uses -- the class default of 20 (~4600 chars)
        # does not.
        large = _make_large_result(n_matches=40)
        with patch(
            "code_indexer.server.mcp.handlers._utils.app_module",
            **{"app": mock_app},
        ):
            helper = _import_helper()
            result = helper(large)

        assert result["cache_unavailable"] is True
        assert result["truncated"] is True
        assert result["cache_handle"] is None
        assert len(result["matches"]) < len(large["matches"]), (
            "cache-unavailable degrade must still BOUND the result -- "
            "returning everything defeats the whole truncation contract"
        )

    def test_cache_none_bounds_a_large_result(self):
        """payload_cache=None on app.state: same bounded degrade as a
        missing attribute."""
        mock_state = MagicMock()
        mock_state.payload_cache = None
        mock_app = MagicMock()
        mock_app.state = mock_state

        large = _make_large_result(n_matches=40)
        with patch(
            "code_indexer.server.mcp.handlers._utils.app_module",
            **{"app": mock_app},
        ):
            helper = _import_helper()
            result = helper(large)

        assert result["cache_unavailable"] is True
        assert result["truncated"] is True
        assert result["cache_handle"] is None
        assert len(result["matches"]) < len(large["matches"])

    def test_cache_unavailable_returns_a_new_dict_not_the_input_object(self):
        """The bounded degrade response is assembled fresh, not the same
        object as the input (unlike the pre-Bug-#1928-round-3 behavior,
        which returned the input dict unchanged/identity-equal)."""
        mock_state = MagicMock(spec=[])
        mock_app = MagicMock()
        mock_app.state = mock_state

        large = _make_large_result()
        large_id = id(large)
        with patch(
            "code_indexer.server.mcp.handlers._utils.app_module",
            **{"app": mock_app},
        ):
            helper = _import_helper()
            result = helper(large)

        assert id(result) != large_id
        # The original input itself must remain unmutated.
        assert "cache_unavailable" not in large
