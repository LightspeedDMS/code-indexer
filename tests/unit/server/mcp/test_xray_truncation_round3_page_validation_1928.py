"""Bug #1928 round 3 (P2 -- Codex): fetch_cached_page requires page >= 1,
no clamping. (The MCP front door's own rejection of non-int `page`
values such as strings/floats/booleans, returning a structured
`invalid_page` error, is covered separately in
tests/unit/server/mcp/test_cidx_fetch_cached_payload.py -- this file
covers fetch_cached_page's own int-typed contract.)

Uses a real, on-disk PayloadCache (cache_factory fixture) -- no mocking.
"""

from __future__ import annotations

import pytest

from code_indexer.server.cache.payload_cache import CacheNotFoundError
from code_indexer.server.mcp.handlers import xray_truncation as xt

from ._xray_truncation_test_helpers import cache_factory, make_match  # noqa: F401

_SMALL_BUDGET_CHARS = 300
_MANY_MATCH_COUNT = 30


class TestPageParameterValidationNoClamp:
    def test_page_zero_raises_value_error(
        self,
        cache_factory,  # noqa: F811 -- pytest fixture injection
    ) -> None:
        cache = cache_factory(max_fetch_size_chars=_SMALL_BUDGET_CHARS)
        with pytest.raises(ValueError):
            xt.fetch_cached_page(cache, "any-handle", 0)

    def test_negative_page_raises_value_error(
        self,
        cache_factory,  # noqa: F811 -- pytest fixture injection
    ) -> None:
        cache = cache_factory(max_fetch_size_chars=_SMALL_BUDGET_CHARS)
        with pytest.raises(ValueError):
            xt.fetch_cached_page(cache, "any-handle", -5)

    def test_page_beyond_total_pages_raises_cache_not_found(
        self,
        cache_factory,  # noqa: F811 -- pytest fixture injection
    ) -> None:
        cache = cache_factory(max_fetch_size_chars=_SMALL_BUDGET_CHARS)
        matches = [make_match(i) for i in range(_MANY_MATCH_COUNT)]
        truncated = xt.truncate_result_fields(
            {"matches": matches, "evaluation_errors": []},
            cache,
            ["matches", "evaluation_errors"],
        )
        beyond = truncated["total_pages"] + 1

        with pytest.raises(CacheNotFoundError):
            xt.fetch_cached_page(cache, truncated["cache_handle"], beyond)
