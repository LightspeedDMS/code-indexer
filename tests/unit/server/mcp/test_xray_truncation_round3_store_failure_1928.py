"""Bug #1928 round 3 (P1 -- Codex REJECT): a page-set write failure must
surface as an explicit error (PageSetStoreError), never as a returned
cache_handle pointing at data that was never durably written.

Failure injected via a real-shaped fake cache whose store_batch_with_keys()
raises -- a controlled double at the external-dependency boundary (the
same pattern already used for the backend-level failure-injection tests
in tests/unit/storage/test_payload_cache_backend_store_batch_strict_1928.py
and tests/unit/server/cache/test_payload_cache_store_batch_with_keys_1928.py),
not a mock of the truncation logic under test.
"""

from __future__ import annotations

from typing import List, Tuple

import pytest

from code_indexer.server.mcp.handlers import xray_truncation as xt

from ._xray_truncation_test_helpers import make_match  # noqa: F401

_SMALL_BUDGET_CHARS = 300
_MANY_MATCH_COUNT = 30


class _FailingStoreBatchCache:
    """A real-shaped fake whose store_batch_with_keys() always raises."""

    def __init__(self, max_fetch_size_chars: int) -> None:
        from code_indexer.server.cache.payload_cache import PayloadCacheConfig

        self.config = PayloadCacheConfig(
            preview_size_chars=max_fetch_size_chars,
            max_fetch_size_chars=max_fetch_size_chars,
        )

    def store_batch_with_keys(self, items: List[Tuple[str, str]]) -> None:
        raise RuntimeError("simulated page-set write failure")


class TestPageSetStoreFailureSurfacesAsError:
    def test_store_failure_raises_page_set_store_error(self) -> None:
        cache = _FailingStoreBatchCache(_SMALL_BUDGET_CHARS)
        matches = [make_match(i) for i in range(_MANY_MATCH_COUNT)]
        result = {"matches": matches, "evaluation_errors": []}

        with pytest.raises(xt.PageSetStoreError, match="simulated page-set"):
            xt.truncate_result_fields(result, cache, ["matches", "evaluation_errors"])
