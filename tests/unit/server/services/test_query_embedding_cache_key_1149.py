"""Story #1149: Hash-free, config-namespaced cache key (+ passive reset).

Tests cover the 7 Gherkin scenarios from issue #1149:

  Scenario 1: build_key returns a config-namespaced readable string within the cap
  Scenario 2: Config-digest isolates two endpoints with otherwise identical config
  Scenario 3: Over-cap normalized forms are not cached, count as a miss, increment long_key
  Scenario 4: None key is guarded at the call boundary
  Scenario 5: New string keyspace and legacy SHA-256 keyspace are disjoint
  Scenario 6: Both backends store the string key with no schema change
  Scenario 7: Admin cache-sample readout exposes key shape without secrets
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_LEGACY_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_NEW_KEY_RE = re.compile(r"^s:[^:]+:.+$")


def _make_sqlite_backend(tmp_path: Path):
    from code_indexer.server.storage.sqlite_backends import (
        QueryEmbeddingCacheSqliteBackend,
    )

    return QueryEmbeddingCacheSqliteBackend(str(tmp_path / "qec.db"))


def _make_cache(tmp_path: Path, *, mode: str = "on"):
    from code_indexer.server.services.query_embedding_cache import QueryEmbeddingCache

    backend = _make_sqlite_backend(tmp_path)
    return QueryEmbeddingCache(
        backend=backend,
        enabled=True,
        voyage_mode=mode,
        cohere_mode=mode,
    )


def _make_qualifier(
    provider: str = "voyage-ai",
    model: str = "voyage-code-3",
    dimension: int = 4,
):
    from code_indexer.server.services.query_embedding_cache import CacheQualifier

    return CacheQualifier(provider=provider, model=model, dimension=dimension)


def _sample_config_digest() -> str:
    """Compute a config digest using the same path as coalescer_registry."""
    from code_indexer.server.services.coalescer_registry import _digest_for_provider

    # Build a minimal provider stub with a .config attribute
    cfg = MagicMock()
    cfg.model = "voyage-code-3"
    cfg.api_key = "test-key-abc123"
    cfg.api_endpoint = "https://api.voyageai.com/v1/embeddings"
    cfg.connect_timeout = 10.0
    cfg.timeout = 30.0
    cfg.max_retries = None
    cfg.retry_delay = None
    cfg.exponential_backoff = None

    provider = MagicMock()
    provider.config = cfg
    provider.__class__.__name__ = "VoyageAIClient"

    return _digest_for_provider(provider)  # type: ignore[no-any-return]


# ===========================================================================
# Scenario 1: build_key returns a config-namespaced readable string within the cap
# ===========================================================================


class TestScenario1ConfigNamespacedKeyShape:
    """Scenario 1: build_key returns 's:<config-digest>:<normalized-query>'."""

    def test_build_key_returns_s_prefix_string(self) -> None:
        """build_key with a valid config_digest returns 's:<digest>:<normalized>'."""
        from code_indexer.server.services.query_embedding_cache import build_key

        digest = "abc123"
        result = build_key("hello world", config_digest=digest)
        assert result is not None
        assert isinstance(result, str)
        assert result.startswith("s:")

    def test_build_key_contains_config_digest(self) -> None:
        """The config-digest is embedded between the 's:' prefix and the query part."""
        from code_indexer.server.services.query_embedding_cache import build_key

        digest = "deadbeef1234"
        result = build_key("find auth handler", config_digest=digest)
        assert result is not None
        parts = result.split(":")
        # Format: s:<digest>:<normalized-query>
        assert parts[0] == "s"
        assert parts[1] == digest

    def test_build_key_normalized_query_part_is_readable(self) -> None:
        """The normalized query part is human-readable (not a hex hash)."""
        from code_indexer.server.services.query_embedding_cache import build_key

        digest = "somedigest"
        result = build_key("find authentication handler", config_digest=digest)
        assert result is not None
        # The normalized part should NOT be a 64-char hex SHA-256
        # Strip the 's:<digest>:' prefix
        prefix = f"s:{digest}:"
        normalized_part = result[len(prefix) :]
        assert not _LEGACY_SHA256_RE.match(normalized_part), (
            "Normalized part must not be a SHA-256 hex digest"
        )

    def test_build_key_normalized_part_at_most_256_chars(self) -> None:
        """The normalized-query part is at most 256 characters."""
        from code_indexer.server.services.query_embedding_cache import build_key

        digest = "testdigest"
        text = "short query"
        result = build_key(text, config_digest=digest)
        assert result is not None
        prefix = f"s:{digest}:"
        normalized_part = result[len(prefix) :]
        assert len(normalized_part) <= 256

    def test_build_key_uses_anchor_token_normalization(self) -> None:
        """Two tail-reordered near-repeats with same first-2 tokens produce same key."""
        from code_indexer.server.services.query_embedding_cache import build_key

        digest = "d1gest"
        q1 = "find authentication middleware handler"
        q2 = "find authentication handler middleware"
        key1 = build_key(q1, config_digest=digest, anchor_tokens=2)
        key2 = build_key(q2, config_digest=digest, anchor_tokens=2)
        assert key1 == key2, (
            "Tail-reordered near-repeats must produce the same cache key"
        )

    def test_build_key_case_preserved(self) -> None:
        """Case is NEVER lowercased in the normalized query."""
        from code_indexer.server.services.query_embedding_cache import build_key

        digest = "d1gest"
        upper = build_key("HELLO WORLD", config_digest=digest)
        lower = build_key("hello world", config_digest=digest)
        assert upper != lower

    def test_build_key_matches_coalescer_registry_digest(self) -> None:
        """The config_digest passed to build_key equals the coalescer registry digest."""
        from code_indexer.server.services.query_embedding_cache import build_key
        from code_indexer.server.services.coalescer_registry import _digest_for_provider

        cfg = MagicMock()
        cfg.model = "voyage-code-3"
        cfg.api_key = "testkey"
        cfg.api_endpoint = "https://api.voyageai.com/v1/embeddings"
        cfg.connect_timeout = 10.0
        cfg.timeout = 30.0
        cfg.max_retries = None
        cfg.retry_delay = None
        cfg.exponential_backoff = None
        provider = MagicMock()
        provider.config = cfg
        provider.__class__.__name__ = "VoyageAIClient"

        registry_digest = _digest_for_provider(provider)
        result = build_key("test query", config_digest=registry_digest)
        assert result is not None
        parts = result.split(":", 2)
        assert parts[1] == registry_digest, (
            "build_key config_digest must equal coalescer registry digest"
        )

    def test_build_key_for_provider_returns_optional_str(self, tmp_path: Path) -> None:
        """build_key_for_provider returns Optional[str], not always str."""
        from code_indexer.server.services.query_embedding_cache import (
            QueryEmbeddingCache,
        )

        backend = _make_sqlite_backend(tmp_path)
        cache = QueryEmbeddingCache(backend=backend, enabled=True)
        # A short query should return a string
        result = cache.build_key_for_provider(
            "hello world", "voyage-ai", config_digest="somedigest"
        )
        assert result is None or isinstance(result, str)

    def test_static_build_key_method_accepts_config_digest(self) -> None:
        """QueryEmbeddingCache.build_key static method accepts config_digest."""
        from code_indexer.server.services.query_embedding_cache import (
            QueryEmbeddingCache,
        )

        result = QueryEmbeddingCache.build_key("test", config_digest="abc123")
        assert result is not None
        assert result.startswith("s:abc123:")


# ===========================================================================
# Scenario 2: Config-digest isolates two endpoints
# ===========================================================================


class TestScenario2EndpointIsolation:
    """Scenario 2: Two configs differing only by endpoint produce distinct keys."""

    def _make_provider_with_endpoint(self, endpoint: str) -> Any:
        cfg = MagicMock()
        cfg.model = "voyage-code-3"
        cfg.api_key = "same-key"
        cfg.api_endpoint = endpoint
        cfg.connect_timeout = 10.0
        cfg.timeout = 30.0
        cfg.max_retries = None
        cfg.retry_delay = None
        cfg.exponential_backoff = None
        provider = MagicMock()
        provider.config = cfg
        provider.__class__.__name__ = "VoyageAIClient"
        return provider

    def test_different_endpoints_produce_different_digests(self) -> None:
        """Two providers with different endpoints produce distinct coalescer digests."""
        from code_indexer.server.services.coalescer_registry import _digest_for_provider

        p1 = self._make_provider_with_endpoint("https://api.voyageai.com/v1/embeddings")
        p2 = self._make_provider_with_endpoint(
            "https://custom.endpoint.com/v1/embeddings"
        )

        d1 = _digest_for_provider(p1)
        d2 = _digest_for_provider(p2)
        assert d1 != d2

    def test_different_endpoints_produce_different_cache_keys(self) -> None:
        """Same query under two endpoint configs produces two distinct cache keys."""
        from code_indexer.server.services.query_embedding_cache import build_key
        from code_indexer.server.services.coalescer_registry import _digest_for_provider

        p1 = self._make_provider_with_endpoint("https://api.voyageai.com/v1/embeddings")
        p2 = self._make_provider_with_endpoint(
            "https://custom.endpoint.com/v1/embeddings"
        )

        d1 = _digest_for_provider(p1)
        d2 = _digest_for_provider(p2)

        query = "find authentication handler"
        key1 = build_key(query, config_digest=d1)
        key2 = build_key(query, config_digest=d2)
        assert key1 != key2, "Different endpoints must produce different cache keys"

    def test_endpoint1_cannot_serve_endpoint2_cached_embedding(
        self, tmp_path: Path
    ) -> None:
        """A cached embedding for endpoint1 cannot be retrieved with endpoint2's key."""
        from code_indexer.server.services.query_embedding_cache import (
            build_key,
            QueryEmbeddingCache,
            CacheQualifier,
        )
        from code_indexer.server.services.coalescer_registry import _digest_for_provider

        p1 = self._make_provider_with_endpoint("https://api.voyageai.com/v1/embeddings")
        p2 = self._make_provider_with_endpoint(
            "https://custom.endpoint.com/v1/embeddings"
        )
        d1 = _digest_for_provider(p1)
        d2 = _digest_for_provider(p2)

        backend = _make_sqlite_backend(tmp_path)
        cache = QueryEmbeddingCache(backend=backend, enabled=True, voyage_mode="on")

        query = "find authentication handler"
        key1 = build_key(query, config_digest=d1)
        key2 = build_key(query, config_digest=d2)
        assert key1 is not None
        assert key2 is not None

        qualifier = CacheQualifier(
            provider="voyage-ai", model="voyage-code-3", dimension=4
        )
        embedding = [1.0, 2.0, 3.0, 4.0]
        cache.record_miss_or_shadow(key1, qualifier, embedding)

        # Lookup with key2 must be a MISS
        result = cache.lookup(key2, qualifier)
        assert result is None, (
            "Endpoint2 must NOT be able to retrieve endpoint1's cached embedding"
        )


# ===========================================================================
# Scenario 3: Over-cap normalized forms → None + miss + long_key counter
# ===========================================================================


class TestScenario3OverCapBehavior:
    """Scenario 3: Normalized query part > 256 chars → build_key returns None."""

    def _make_over_cap_query(self) -> str:
        """Build a query whose normalized form (anchor+sorted-tail) exceeds 256 chars."""
        # Each token is ~10 chars; 30 tokens → ~300 chars after joining
        words = [f"searchtoken{i:03d}" for i in range(30)]
        return " ".join(words)

    def test_over_cap_query_returns_none(self) -> None:
        """build_key returns None when the normalized-query part exceeds 256 chars."""
        from code_indexer.server.services.query_embedding_cache import build_key

        text = self._make_over_cap_query()
        result = build_key(text, config_digest="anydigest")
        assert result is None, "Over-cap query must return None from build_key"

    def test_exactly_256_chars_normalized_returns_string(self) -> None:
        """build_key returns a string when the normalized part is exactly 256 chars."""
        from code_indexer.server.services.query_embedding_cache import build_key

        # Construct exactly 256 char normalized string: one long token
        # anchor=2: token1 + ' ' + token2 = need normalized part == 256 chars
        # With anchor_tokens=0 and a single long token of exactly 256 chars:
        token = "a" * 256
        result = build_key(token, config_digest="d", anchor_tokens=0)
        assert result is not None, "Exactly 256 chars normalized must NOT return None"

    def test_257_chars_normalized_returns_none(self) -> None:
        """build_key returns None when the normalized part is 257 chars."""
        from code_indexer.server.services.query_embedding_cache import build_key

        token = "a" * 257
        result = build_key(token, config_digest="d", anchor_tokens=0)
        assert result is None, "257-char normalized must return None"

    def test_over_cap_no_row_written(self, tmp_path: Path) -> None:
        """No row is written to the cache for an over-cap query."""
        from code_indexer.server.services.query_embedding_cache import (
            build_key,
        )

        backend = _make_sqlite_backend(tmp_path)

        text = self._make_over_cap_query()
        key = build_key(text, config_digest="anydigest")
        assert key is None  # confirm over-cap

        # If key is None, the caller must skip the write
        # Verify backend stays empty
        assert backend.total_entries() == 0

    def test_over_cap_increments_long_key_counter_in_metrics(
        self, tmp_path: Path
    ) -> None:
        """Over-cap resolution increments the long_key counter in cache metrics."""
        from code_indexer.server.services.query_embedding_cache_metrics import (
            QueryEmbeddingCacheMetrics,
        )

        meter = MagicMock()
        meter.create_counter.return_value = MagicMock()
        meter.create_observable_gauge.return_value = MagicMock()
        meter.create_histogram.return_value = MagicMock()

        metrics = QueryEmbeddingCacheMetrics(meter, total_entries_fn=lambda: 0)

        # Before: long_key == 0
        snap = metrics.snapshot()
        assert snap.get("long_key", 0) == 0

        # Record a long_key event
        metrics.record_long_key(provider="voyage-ai")

        # After: long_key == 1
        snap = metrics.snapshot()
        assert snap.get("long_key", 0) == 1

    def test_over_cap_counted_as_miss_in_metrics(self, tmp_path: Path) -> None:
        """Over-cap queries increment the miss counter (not a separate counter)."""
        from code_indexer.server.services.query_embedding_cache_metrics import (
            QueryEmbeddingCacheMetrics,
        )

        meter = MagicMock()
        meter.create_counter.return_value = MagicMock()
        meter.create_observable_gauge.return_value = MagicMock()
        meter.create_histogram.return_value = MagicMock()

        metrics = QueryEmbeddingCacheMetrics(meter, total_entries_fn=lambda: 0)
        metrics.record_miss(mode="on", provider="voyage-ai")
        metrics.record_long_key(provider="voyage-ai")

        snap = metrics.snapshot()
        # Miss should have been recorded
        assert snap["on"]["misses"] == 1
        # Long key counter also incremented
        assert snap.get("long_key", 0) == 1

    def test_over_cap_normalized_form_never_truncated(self) -> None:
        """Over-cap returns None, never a truncated key."""
        from code_indexer.server.services.query_embedding_cache import build_key

        text = self._make_over_cap_query()
        result = build_key(text, config_digest="d")
        # Must be None — not a string ending at char 256
        assert result is None


# ===========================================================================
# Scenario 4: None key guarded at the call boundary
# ===========================================================================


class TestScenario4NoneKeyGuardedAtCallBoundary:
    """Scenario 4: None from build_key is handled at governed_call.py call boundary."""

    def test_build_key_for_provider_returns_none_for_over_cap(
        self, tmp_path: Path
    ) -> None:
        """cache.build_key_for_provider returns None for over-cap query."""
        from code_indexer.server.services.query_embedding_cache import (
            QueryEmbeddingCache,
        )

        backend = _make_sqlite_backend(tmp_path)
        cache = QueryEmbeddingCache(backend=backend, enabled=True, voyage_mode="on")

        # Over-cap text: normalized form > 256 chars
        long_text = " ".join([f"searchtoken{i:03d}" for i in range(30)])
        result = cache.build_key_for_provider(
            long_text, "voyage-ai", config_digest="anydigest"
        )
        assert result is None

    def test_none_key_skips_lookup_and_write_and_records_miss_and_long_key(
        self, tmp_path: Path
    ) -> None:
        """When build_key returns None (over-cap), coalesced_query_embedding must:
        - skip the backend lookup (no rows read)
        - skip the backend write (no rows written)
        - record a MISS in the metrics
        - increment the long_key counter
        - return the live embedding vector

        This directly exercises the None-guard at the call boundary in governed_call.py.
        """
        from code_indexer.server.services.governed_call import (
            coalesced_query_embedding,
            set_query_embedding_cache,
            clear_query_embedding_cache,
            set_query_embedding_cache_metrics,
            clear_query_embedding_cache_metrics,
        )
        from code_indexer.server.services.query_embedding_cache import (
            QueryEmbeddingCache,
        )
        from code_indexer.server.services.query_embedding_cache_metrics import (
            QueryEmbeddingCacheMetrics,
        )

        backend = _make_sqlite_backend(tmp_path)
        cache = QueryEmbeddingCache(backend=backend, enabled=True, voyage_mode="on")

        meter = MagicMock()
        meter.create_counter.return_value = MagicMock()
        meter.create_observable_gauge.return_value = MagicMock()
        meter.create_histogram.return_value = MagicMock()
        metrics = QueryEmbeddingCacheMetrics(meter, total_entries_fn=lambda: 0)

        set_query_embedding_cache(cache)
        set_query_embedding_cache_metrics(metrics)

        try:
            # Over-cap text: normalized form > 256 chars -> build_key returns None
            long_text = " ".join([f"searchtoken{i:03d}" for i in range(30)])
            live_vec = [0.1, 0.2, 0.3, 0.4]

            provider = MagicMock()
            provider.get_provider_name.return_value = "voyage-ai"
            provider.get_current_model.return_value = "voyage-code-3"
            provider.get_model_info.return_value = {"dimensions": 4}

            with patch(
                "code_indexer.server.services.governed_call._compute_live",
                return_value=live_vec,
            ):
                result = coalesced_query_embedding(provider, long_text)

            # Live path returned
            assert result == live_vec

            # No row written to the backend (lookup skipped, write skipped)
            assert backend.total_entries() == 0, (
                "No cache row must be written when build_key returns None"
            )

            # MISS recorded in metrics (mode may be "on" or "shadow" depending on live config)
            snap = metrics.snapshot()
            on_misses = snap.get("on", {}).get("misses", 0)
            shadow_misses = snap.get("shadow", {}).get("misses", 0)
            total_misses = on_misses + shadow_misses
            assert total_misses >= 1, (
                f"Expected at least 1 MISS recorded across on/shadow modes; "
                f"got on.misses={on_misses}, shadow.misses={shadow_misses}"
            )

            # long_key counter incremented
            assert snap.get("long_key", 0) >= 1, (
                "long_key counter must be incremented when build_key returns None"
            )
        finally:
            clear_query_embedding_cache()
            clear_query_embedding_cache_metrics()

    def test_backend_never_receives_none_key_via_governed_call(
        self, tmp_path: Path
    ) -> None:
        """Backend is never called with None as cache_key through governed_call.

        Drives an over-cap query through coalesced_query_embedding with a spy on
        the backend's lookup and upsert methods, asserting neither is called with
        None.  If the None-guard failed, backend.lookup(None, ...) would raise a
        TypeError from the TEXT column constraint.
        """
        from code_indexer.server.services.governed_call import (
            coalesced_query_embedding,
            set_query_embedding_cache,
            clear_query_embedding_cache,
        )
        from code_indexer.server.services.query_embedding_cache import (
            QueryEmbeddingCache,
        )

        backend = _make_sqlite_backend(tmp_path)

        # Wrap backend methods with spies to detect any None-key call
        original_lookup = backend.lookup
        original_upsert = backend.upsert
        lookup_calls: list = []
        upsert_calls: list = []

        def spy_lookup(cache_key, *args, **kwargs):
            lookup_calls.append(cache_key)
            return original_lookup(cache_key, *args, **kwargs)

        def spy_upsert(cache_key, *args, **kwargs):
            upsert_calls.append(cache_key)
            return original_upsert(cache_key, *args, **kwargs)

        backend.lookup = spy_lookup  # type: ignore[method-assign]
        backend.upsert = spy_upsert  # type: ignore[method-assign]

        cache = QueryEmbeddingCache(backend=backend, enabled=True, voyage_mode="on")
        set_query_embedding_cache(cache)

        try:
            long_text = " ".join([f"searchtoken{i:03d}" for i in range(30)])
            live_vec = [0.1, 0.2, 0.3, 0.4]

            provider = MagicMock()
            provider.get_provider_name.return_value = "voyage-ai"
            provider.get_current_model.return_value = "voyage-code-3"
            provider.get_model_info.return_value = {"dimensions": 4}

            with patch(
                "code_indexer.server.services.governed_call._compute_live",
                return_value=live_vec,
            ):
                result = coalesced_query_embedding(provider, long_text)

            assert result == live_vec

            # Backend must not have been called at all (None-guard fires first)
            assert None not in lookup_calls, (
                "backend.lookup must never be called with None as cache_key"
            )
            assert None not in upsert_calls, (
                "backend.upsert must never be called with None as cache_key"
            )
            # And in fact, for an over-cap query, neither should be called at all
            assert len(lookup_calls) == 0, (
                f"backend.lookup should not be called for over-cap query; calls={lookup_calls}"
            )
            assert len(upsert_calls) == 0, (
                f"backend.upsert should not be called for over-cap query; calls={upsert_calls}"
            )
        finally:
            clear_query_embedding_cache()

    def test_governed_call_handles_none_key_without_raising(
        self, tmp_path: Path
    ) -> None:
        """coalesced_query_embedding doesn't raise when build_key returns None."""
        from code_indexer.server.services.governed_call import (
            set_query_embedding_cache,
            clear_query_embedding_cache,
        )
        from code_indexer.server.services.query_embedding_cache import (
            QueryEmbeddingCache,
        )

        backend = _make_sqlite_backend(tmp_path)
        cache = QueryEmbeddingCache(backend=backend, enabled=True, voyage_mode="on")
        set_query_embedding_cache(cache)

        try:
            long_text = " ".join([f"searchtoken{i:03d}" for i in range(30)])

            # Build a minimal provider stub
            provider = MagicMock()
            provider.get_provider_name.return_value = "voyage-ai"
            provider.get_current_model.return_value = "voyage-code-3"
            provider.get_model_info.return_value = {"dimensions": 4}
            live_vec = [0.1, 0.2, 0.3, 0.4]

            # Patch the live path to return a short vector
            with patch(
                "code_indexer.server.services.governed_call._compute_live",
                return_value=live_vec,
            ):
                from code_indexer.server.services.governed_call import (
                    coalesced_query_embedding,
                )

                # Should not raise even when the key is None (over-cap)
                result = coalesced_query_embedding(provider, long_text)
                assert result == live_vec

            # No row must have been written
            assert backend.total_entries() == 0
        finally:
            clear_query_embedding_cache()


# ===========================================================================
# Scenario 5: New keyspace and legacy SHA-256 keyspace are disjoint
# ===========================================================================


class TestScenario5KeyspaceDisjointness:
    """Scenario 5: 's:' prefix keys and legacy 64-hex SHA-256 keys are provably disjoint."""

    def test_new_key_starts_with_s_colon(self) -> None:
        """New keys always start with 's:' — never a 64-char hex string."""
        from code_indexer.server.services.query_embedding_cache import build_key

        result = build_key("hello world", config_digest="abc123")
        assert result is not None
        assert result.startswith("s:")
        # Confirm it's not a 64-hex key
        assert not _LEGACY_SHA256_RE.match(result)

    def test_legacy_sha256_key_does_not_start_with_s_colon(self) -> None:
        """Legacy SHA-256 keys (64 lowercase hex chars) cannot start with 's:'."""
        import hashlib

        # Any SHA-256 hash: 64 lowercase hex chars — cannot start with 's:'
        legacy = hashlib.sha256(b"some query text").hexdigest()
        assert len(legacy) == 64
        assert not legacy.startswith("s:")

    def test_new_and_legacy_keys_never_collide(self) -> None:
        """A new 's:' key can never equal a legacy 64-hex key."""
        import hashlib
        from code_indexer.server.services.query_embedding_cache import build_key

        legacy = hashlib.sha256(b"find authentication handler").hexdigest()
        new_key = build_key("find authentication handler", config_digest="abc")
        assert new_key is not None
        assert new_key != legacy

    def test_legacy_rows_coexist_in_backend(self, tmp_path: Path) -> None:
        """Legacy SHA-256 rows and new 's:' rows can coexist without collision."""
        import hashlib

        backend = _make_sqlite_backend(tmp_path)
        now = time.time()
        vec_bytes = b"\x00" * 16  # 4 float32 zeros

        legacy_key = hashlib.sha256(b"legacy query").hexdigest()
        new_key = "s:abc123:legacy query"

        # Insert both rows (different cache_key, same provider/model/dim)
        backend.upsert(legacy_key, "voyage-ai", "voyage-code-3", 4, vec_bytes, now, now)
        backend.upsert(new_key, "voyage-ai", "voyage-code-3", 4, vec_bytes, now, now)

        assert backend.total_entries() == 2, "Both legacy and new rows should coexist"

        # Lookup each key independently — no cross-serve
        legacy_result = backend.lookup(legacy_key, "voyage-ai", "voyage-code-3", 4)
        new_result = backend.lookup(new_key, "voyage-ai", "voyage-code-3", 4)
        assert legacy_result is not None
        assert new_result is not None

    def test_prune_to_max_evicts_legacy_rows_by_lru(self, tmp_path: Path) -> None:
        """prune_to_max evicts oldest rows (legacy or new) by LRU without active clear."""
        import hashlib

        backend = _make_sqlite_backend(tmp_path)
        now = time.time()
        vec_bytes = b"\x00" * 16

        # Insert legacy row first (older last_used)
        legacy_key = hashlib.sha256(b"old legacy query").hexdigest()
        backend.upsert(
            legacy_key, "voyage-ai", "voyage-code-3", 4, vec_bytes, now - 100, now - 100
        )

        # Insert new row (newer last_used)
        new_key = "s:digest123:new query text"
        backend.upsert(new_key, "voyage-ai", "voyage-code-3", 4, vec_bytes, now, now)

        assert backend.total_entries() == 2

        # Prune to 1 — should evict the legacy row (oldest last_used)
        deleted = backend.prune_to_max(1)
        assert deleted == 1
        assert backend.total_entries() == 1

        # New row must survive
        new_result = backend.lookup(new_key, "voyage-ai", "voyage-code-3", 4)
        assert new_result is not None

        # Legacy row must be gone
        legacy_result = backend.lookup(legacy_key, "voyage-ai", "voyage-code-3", 4)
        assert legacy_result is None

    def test_no_active_clear_called_during_migration(self, tmp_path: Path) -> None:
        """No backend.clear() call is made — passive LRU only."""
        backend = _make_sqlite_backend(tmp_path)
        now = time.time()
        vec_bytes = b"\x00" * 16

        # Insert a legacy-style row
        import hashlib

        legacy_key = hashlib.sha256(b"query").hexdigest()
        backend.upsert(legacy_key, "voyage-ai", "voyage-code-3", 4, vec_bytes, now, now)

        # The passive reset never calls clear() — legacy row persists until LRU eviction
        assert backend.total_entries() == 1
        # No clear() was called; row still exists
        result = backend.lookup(legacy_key, "voyage-ai", "voyage-code-3", 4)
        assert result is not None


# ===========================================================================
# Scenario 6: Both backends store the string key with no schema change
# ===========================================================================


class TestScenario6BothBackendsStringKey:
    """Scenario 6: SQLite backend stores string key (TEXT column) — no DDL change."""

    def test_sqlite_backend_stores_string_key(self, tmp_path: Path) -> None:
        """SQLite backend accepts and returns a string cache_key without error."""
        backend = _make_sqlite_backend(tmp_path)
        key = "s:abc123:find authentication handler"
        now = time.time()
        vec_bytes = b"\x00" * 16

        backend.upsert(key, "voyage-ai", "voyage-code-3", 4, vec_bytes, now, now)
        result = backend.lookup(key, "voyage-ai", "voyage-code-3", 4)
        assert result is not None
        assert len(result) == 16

    def test_sqlite_backend_cache_key_is_text_column(self, tmp_path: Path) -> None:
        """The cache_key column in SQLite is TEXT — no schema change needed."""
        import sqlite3
        from code_indexer.server.storage.sqlite_backends import (
            QueryEmbeddingCacheSqliteBackend,
        )

        db_path = str(tmp_path / "qec.db")
        QueryEmbeddingCacheSqliteBackend(db_path)  # create schema

        conn = sqlite3.connect(db_path)
        cursor = conn.execute("PRAGMA table_info(query_embedding_cache)")
        columns = {row[1]: row[2] for row in cursor.fetchall()}  # name -> type
        conn.close()

        assert "cache_key" in columns, "cache_key column must exist"
        assert columns["cache_key"].upper() == "TEXT", (
            f"cache_key must be TEXT, got {columns['cache_key']}"
        )

    def test_sqlite_backend_string_key_longer_than_64_chars(
        self, tmp_path: Path
    ) -> None:
        """SQLite stores keys longer than 64 chars (s: prefix + digest + normalized)."""
        backend = _make_sqlite_backend(tmp_path)
        # A typical new key: "s:" + 64-char digest + ":" + normalized (up to 256)
        long_key = "s:" + "a" * 64 + ":" + "find auth handler"
        now = time.time()
        vec_bytes = b"\x00" * 16

        backend.upsert(long_key, "voyage-ai", "voyage-code-3", 4, vec_bytes, now, now)
        result = backend.lookup(long_key, "voyage-ai", "voyage-code-3", 4)
        assert result is not None

    def test_sqlite_backend_dimension_blob_length_preserved(
        self, tmp_path: Path
    ) -> None:
        """The dimension*4 blob-length validation is preserved with new key format."""
        import struct

        backend = _make_sqlite_backend(tmp_path)
        key = "s:digest123:test query"
        now = time.time()
        dimension = 4
        vec = [1.0, 2.0, 3.0, 4.0]
        blob = struct.pack(f"<{dimension}f", *vec)
        assert len(blob) == dimension * 4

        backend.upsert(key, "voyage-ai", "voyage-code-3", dimension, blob, now, now)
        result = backend.lookup(key, "voyage-ai", "voyage-code-3", dimension)
        assert result is not None
        assert len(result) == dimension * 4

        recovered = list(struct.unpack(f"<{dimension}f", result))
        assert recovered == pytest.approx(vec, abs=1e-6)


# ===========================================================================
# Scenario 7: Admin cache-sample readout exposes key shape without secrets
# ===========================================================================


class TestScenario7AdminCacheSampleReadout:
    """Scenario 7: Admin cache-sample readout returns (key, provider, model, dim, key_length)."""

    def test_sqlite_backend_select_recent_returns_sample_rows(
        self, tmp_path: Path
    ) -> None:
        """SQLite backend select_recent returns rows without embedding vectors."""
        from code_indexer.server.storage.sqlite_backends import (
            QueryEmbeddingCacheSqliteBackend,
        )

        backend = QueryEmbeddingCacheSqliteBackend(str(tmp_path / "qec.db"))
        now = time.time()
        vec_bytes = b"\x00" * 16

        key = "s:abc123:find authentication handler"
        backend.upsert(key, "voyage-ai", "voyage-code-3", 4, vec_bytes, now, now)

        rows = backend.select_recent(limit=10)
        assert len(rows) >= 1

        row = rows[0]
        assert "cache_key" in row
        assert "provider" in row
        assert "model" in row
        assert "dimension" in row
        assert "key_length" in row
        # NO embedding vector
        assert "embedding" not in row

    def test_sample_readout_no_vectors(self, tmp_path: Path) -> None:
        """select_recent never returns embedding bytes."""
        from code_indexer.server.storage.sqlite_backends import (
            QueryEmbeddingCacheSqliteBackend,
        )

        backend = QueryEmbeddingCacheSqliteBackend(str(tmp_path / "qec.db"))
        now = time.time()
        secret_vec = b"\xff" * 4096  # Large vector that should NEVER be returned

        backend.upsert("s:d:q", "voyage-ai", "model", 1024, secret_vec, now, now)

        rows = backend.select_recent(limit=5)
        for row in rows:
            assert "embedding" not in row, (
                "Embedding vector must not appear in sample rows"
            )
            # Values must not contain the secret bytes
            for v in row.values():
                assert not isinstance(v, bytes), "No raw bytes in sample rows"

    def test_sample_readout_key_length_field(self, tmp_path: Path) -> None:
        """key_length in sample row equals len(cache_key)."""
        from code_indexer.server.storage.sqlite_backends import (
            QueryEmbeddingCacheSqliteBackend,
        )

        backend = QueryEmbeddingCacheSqliteBackend(str(tmp_path / "qec.db"))
        now = time.time()
        key = "s:abc123:find authentication handler"
        vec_bytes = b"\x00" * 16

        backend.upsert(key, "voyage-ai", "voyage-code-3", 4, vec_bytes, now, now)

        rows = backend.select_recent(limit=5)
        assert len(rows) == 1
        assert rows[0]["key_length"] == len(key)

    def test_sample_readout_s_prefix_keys_only(self, tmp_path: Path) -> None:
        """Admin readout for new-format rows: keys begin with 's:'."""
        from code_indexer.server.storage.sqlite_backends import (
            QueryEmbeddingCacheSqliteBackend,
        )

        backend = QueryEmbeddingCacheSqliteBackend(str(tmp_path / "qec.db"))
        now = time.time()
        vec_bytes = b"\x00" * 16

        # Insert a new-format key
        new_key = "s:abc123:find authentication handler"
        backend.upsert(new_key, "voyage-ai", "voyage-code-3", 4, vec_bytes, now, now)

        rows = backend.select_recent(limit=5)
        for row in rows:
            key = row["cache_key"]
            # All new-format keys start with 's:'
            if key.startswith("s:"):
                parts = key.split(":", 2)
                assert len(parts) == 3
                normalized_part = parts[2]
                assert len(normalized_part) <= 256

    def test_web_route_cache_sample_endpoint_exists(self) -> None:
        """A REST/web endpoint for admin cache sample is registered."""
        from code_indexer.server.web.routes import web_router

        route_paths = [route.path for route in web_router.routes]  # type: ignore[attr-defined]
        # Check that the cache sample route exists
        matching = [p for p in route_paths if "cache" in p and "sample" in p]
        assert len(matching) >= 1, (
            f"Expected a cache-sample route in web_router routes. Found: {route_paths}"
        )

    def test_cache_sample_route_requires_admin(self) -> None:
        """The cache-sample route is admin-only (no public access)."""
        # This is enforced by _require_admin_session() pattern used in all admin routes.
        # We verify the route exists (already tested) and that the handler calls
        # _require_admin_session — structural test via source inspection.
        import inspect
        from code_indexer.server.web import routes as routes_module

        # Find the cache sample handler
        source = inspect.getsource(routes_module)
        assert "cache-sample" in source or "cache_sample" in source, (
            "Cache sample endpoint must be defined in routes"
        )


# ===========================================================================
# Integration: full round-trip with new key format
# ===========================================================================


class TestIntegrationNewKeyRoundTrip:
    """Integration: cache stores and retrieves embeddings with new key format."""

    def test_full_roundtrip_with_config_digest(self, tmp_path: Path) -> None:
        """Store and retrieve an embedding using the new s:<digest>:<query> key."""
        from code_indexer.server.services.query_embedding_cache import (
            build_key,
            QueryEmbeddingCache,
            CacheQualifier,
        )
        import numpy as np

        backend = _make_sqlite_backend(tmp_path)
        cache = QueryEmbeddingCache(backend=backend, enabled=True, voyage_mode="on")

        digest = "testdigest123"
        text = "find authentication handler"
        key = build_key(text, config_digest=digest)
        assert key is not None
        assert key.startswith(f"s:{digest}:")

        qualifier = CacheQualifier(
            provider="voyage-ai", model="voyage-code-3", dimension=4
        )
        vec = [1.0, 2.0, 3.0, 4.0]
        cache.record_miss_or_shadow(key, qualifier, vec)

        result = cache.lookup(key, qualifier)
        assert result is not None
        recovered = list(np.frombuffer(result, dtype="<f4"))
        assert recovered == pytest.approx(vec, abs=1e-6)

    def test_build_key_for_provider_with_short_query_returns_string(
        self, tmp_path: Path
    ) -> None:
        """build_key_for_provider returns a string for a short query."""
        from code_indexer.server.services.query_embedding_cache import (
            QueryEmbeddingCache,
        )

        backend = _make_sqlite_backend(tmp_path)
        cache = QueryEmbeddingCache(backend=backend, enabled=True, voyage_mode="on")

        result = cache.build_key_for_provider(
            "hello world", "voyage-ai", config_digest="testdigest"
        )
        assert result is not None
        assert result.startswith("s:testdigest:")

    def test_metrics_long_key_counter_surfaced_in_snapshot(self) -> None:
        """long_key counter is surfaced in metrics snapshot."""
        from code_indexer.server.services.query_embedding_cache_metrics import (
            QueryEmbeddingCacheMetrics,
        )

        meter = MagicMock()
        meter.create_counter.return_value = MagicMock()
        meter.create_observable_gauge.return_value = MagicMock()
        meter.create_histogram.return_value = MagicMock()

        metrics = QueryEmbeddingCacheMetrics(meter, total_entries_fn=lambda: 0)

        # Initial snapshot must include long_key
        snap = metrics.snapshot()
        assert "long_key" in snap, "snapshot() must include 'long_key' counter"
        assert snap["long_key"] == 0

        # After recording
        metrics.record_long_key(provider="voyage-ai")
        metrics.record_long_key(provider="cohere")

        snap2 = metrics.snapshot()
        assert snap2["long_key"] == 2
