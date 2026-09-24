"""Bug #1928 round 3 (P2 -- Codex): manifest detection by a dedicated
handle-prefix discriminator (never content-shape sniffing), with strict
validation -- a handle that carries the prefix but fails validation
raises MalformedManifestError loudly instead of silently degrading.

Uses a real, on-disk PayloadCache (cache_factory fixture) -- no mocking.
cache_factory's own teardown closes every cache it creates.
"""

from __future__ import annotations

import json

import pytest

from code_indexer.server.mcp.handlers import xray_truncation as xt

from ._xray_truncation_test_helpers import cache_factory  # noqa: F401

_SMALL_BUDGET_CHARS = 300


class TestManifestDiscriminatorAndValidation:
    def test_legacy_handle_never_reaches_manifest_parsing(
        self,
        cache_factory,  # noqa: F811 -- pytest fixture injection
    ) -> None:
        """A plain (non-prefixed) handle whose content HAPPENS to be a
        JSON object shaped like a manifest must still be served via the
        legacy windowed path, never mistaken for a pages-v1 manifest."""
        cache = cache_factory(max_fetch_size_chars=_SMALL_BUDGET_CHARS)
        json_shaped_content = json.dumps({"format": "pages-v1", "total_pages": 99})
        legacy_handle = cache.store(json_shaped_content)

        result = xt.fetch_cached_page(cache, legacy_handle, 1)

        assert result["content"] == json_shaped_content[:_SMALL_BUDGET_CHARS]

    def test_prefixed_handle_with_malformed_content_raises_loudly(
        self,
        cache_factory,  # noqa: F811 -- pytest fixture injection
    ) -> None:
        cache = cache_factory(max_fetch_size_chars=_SMALL_BUDGET_CHARS)
        bad_handle = f"{xt._PAGES_V1_HANDLE_PREFIX}corrupt-handle"
        cache.store_with_key(bad_handle, "not even json")

        with pytest.raises(xt.MalformedManifestError):
            xt.fetch_cached_page(cache, bad_handle, 1)

    def test_prefixed_handle_with_mismatched_page_count_raises_loudly(
        self,
        cache_factory,  # noqa: F811 -- pytest fixture injection
    ) -> None:
        cache = cache_factory(max_fetch_size_chars=_SMALL_BUDGET_CHARS)
        bad_handle = f"{xt._PAGES_V1_HANDLE_PREFIX}mismatch-handle"
        manifest = {
            "format": "pages-v1",
            "total_pages": 3,
            "page_handles": ["only-one-handle"],
        }
        cache.store_with_key(bad_handle, json.dumps(manifest))

        with pytest.raises(xt.MalformedManifestError):
            xt.fetch_cached_page(cache, bad_handle, 1)
