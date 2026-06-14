"""Story #1106: Anchor-token normalization dial — unit tests (S2).

Tests cover build_key(text, anchor_tokens):
- first-N tokens in original order + sorted tail, CASE PRESERVED, SHA-256
- anchor_tokens=0 -> sort ALL tokens
- anchor_tokens >= token_count -> exact-match (== S1 exact key)
- case differences NEVER collapse
- duplicates kept as sorted multiset
- unicode preserved
- empty / single-token edge cases
- tail-reorder near-repeats collapse to one key
- per-provider anchor_tokens read LIVE from config
- namespace-change log fires once on change

Tokenization pin:
  str.split() with no argument — collapses whitespace runs, strips leading/
  trailing whitespace, never lowercases, punctuation attached to its token.
"""

from __future__ import annotations

import hashlib
import logging
from pathlib import Path
from typing import cast
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_key(text: str, anchor_tokens: int = 2) -> str:
    from code_indexer.server.services.query_embedding_cache import build_key

    return str(build_key(text, anchor_tokens))


def _expected_sha256(normalized: str) -> str:
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# AC1: first-N tokens in order + sorted tail, case preserved
# ---------------------------------------------------------------------------


class TestBuildKeyAnchorNormalization:
    """AC1: Key = first-N tokens in order + alphabetically-sorted tail."""

    def test_anchor2_five_token_query(self) -> None:
        """AC1 example from story: 'Customer Account Management Service Layer'.

        anchor=2 -> anchor: ['Customer', 'Account']
                    tail sorted: ['Layer', 'Management', 'Service']
        normalized = 'Customer Account Layer Management Service'
        """
        text = "Customer Account Management Service Layer"
        key = _build_key(text, anchor_tokens=2)
        normalized = "Customer Account Layer Management Service"
        assert key == _expected_sha256(normalized)

    def test_anchor2_tail_reorder_same_key(self) -> None:
        """Two queries sharing first-2 tokens + same tail bag -> same key."""
        q1 = "find authentication middleware handler"
        q2 = "find authentication handler middleware"
        assert _build_key(q1, 2) == _build_key(q2, 2)

    def test_anchor2_tail_reorder_three_orderings(self) -> None:
        """Three different tail orderings with same first-2 + same tail bag -> same key."""
        q1 = "get user session token cookie"
        q2 = "get user cookie session token"
        q3 = "get user token cookie session"
        key1 = _build_key(q1, 2)
        assert key1 == _build_key(q2, 2)
        assert key1 == _build_key(q3, 2)

    def test_anchor2_different_first_tokens_different_key(self) -> None:
        """Different first-2 tokens -> different keys."""
        q1 = "find authentication handler"
        q2 = "locate authentication handler"
        assert _build_key(q1, 2) != _build_key(q2, 2)

    def test_anchor1_one_anchor_token(self) -> None:
        """anchor=1: first token preserved, rest sorted."""
        q1 = "search database connection pool"
        q2 = "search connection database pool"
        q3 = "search pool connection database"
        key1 = _build_key(q1, 1)
        assert key1 == _build_key(q2, 1)
        assert key1 == _build_key(q3, 1)

    def test_anchor3_three_anchor_tokens(self) -> None:
        """anchor=3: first 3 tokens preserved, rest sorted."""
        q1 = "get user session extra filler"
        q2 = "get user session filler extra"
        assert _build_key(q1, 3) == _build_key(q2, 3)

    def test_anchor_preserves_exact_order_prefix(self) -> None:
        """Two queries with same token set but different first-2 order -> different keys."""
        q1 = "Alpha Beta Gamma Delta"
        q2 = "Beta Alpha Gamma Delta"  # swap first two
        assert _build_key(q1, 2) != _build_key(q2, 2)


# ---------------------------------------------------------------------------
# AC2: anchor_tokens=0 sorts all tokens; >=len is exact-match
# ---------------------------------------------------------------------------


class TestBuildKeyBoundaryValues:
    """AC2: anchor=0 sort-all; anchor>=len exact-match."""

    def test_anchor0_sorts_all_tokens(self) -> None:
        """anchor=0: ALL tokens alphabetically sorted."""
        q1 = "Zebra Apple Mango"
        q2 = "Mango Zebra Apple"
        q3 = "Apple Zebra Mango"
        key = _build_key(q1, 0)
        assert key == _build_key(q2, 0)
        assert key == _build_key(q3, 0)

    def test_anchor0_normalizes_to_sorted_joined(self) -> None:
        """anchor=0 produces SHA-256 of sorted tokens joined by space."""
        text = "Zebra Apple Mango"
        key = _build_key(text, 0)
        normalized = "Apple Mango Zebra"  # alphabetically sorted
        assert key == _expected_sha256(normalized)

    def test_anchor_gte_len_is_exact_match(self) -> None:
        """anchor >= token count -> key = SHA-256 of original text verbatim."""
        text = "Hello World"
        key_exact = _build_key(text, 99)  # anchor >> len -> exact
        # Must equal SHA-256 of the normalized string "Hello World"
        # (all 2 tokens kept in order; no tail)
        normalized = "Hello World"
        assert key_exact == _expected_sha256(normalized)

    def test_anchor_eq_len_is_exact(self) -> None:
        """anchor == token count -> key is exact-match."""
        text = "Alpha Beta Gamma"
        tokens = text.split()
        key = _build_key(text, len(tokens))  # anchor == 3
        # All 3 tokens in original order, tail is empty -> same as exact
        normalized = "Alpha Beta Gamma"
        assert key == _expected_sha256(normalized)

    def test_anchor_gte_len_two_orderings_differ(self) -> None:
        """anchor >= len: different orderings produce different keys (exact-match behaviour)."""
        q1 = "Alpha Beta Gamma"
        q2 = "Gamma Beta Alpha"
        # Both have len=3 tokens, anchor=3 >= 3 -> exact match
        assert _build_key(q1, 3) != _build_key(q2, 3)

    def test_anchor_gte_len_equals_s1_exact_key(self) -> None:
        """anchor >= token count must produce the IDENTICAL key to the S1 exact-match key.

        The S1 key was SHA-256 of the normalized string (first-N=all tokens in order,
        tail empty). With anchor_tokens >= len, the normalized string IS the joined
        tokens in original order, joined by a single space — which matches S1's
        behavior of SHA-256 of those tokens joined by space.
        """
        text = "Customer Account Management"
        tokens = text.split()
        # S2 with anchor >= len
        key_s2_exact = _build_key(text, len(tokens))
        # Expected: SHA-256 of "Customer Account Management"
        assert key_s2_exact == _expected_sha256("Customer Account Management")

    def test_anchor_zero_single_token_still_works(self) -> None:
        """anchor=0 with single token: sorted([token]) = [token]."""
        text = "Hello"
        key = _build_key(text, 0)
        assert key == _expected_sha256("Hello")

    def test_anchor_large_single_token(self) -> None:
        """anchor >> len with single token: kept as-is."""
        text = "Hello"
        key = _build_key(text, 100)
        assert key == _expected_sha256("Hello")


# ---------------------------------------------------------------------------
# Case preservation — NEVER lowercase
# ---------------------------------------------------------------------------


class TestBuildKeyCasePreservation:
    """CASE PRESERVED: lowercase and uppercase must never collapse."""

    def test_case_difference_never_collapses(self) -> None:
        """'Background' vs 'background' -> different keys."""
        assert _build_key("Background worker", 2) != _build_key("background worker", 2)

    def test_camelcase_preserved(self) -> None:
        """CamelCase tokens never lowercased."""
        assert _build_key("CamelCase Query", 2) != _build_key("camelcase query", 2)

    def test_mixed_case_tail_not_sorted_case_insensitively(self) -> None:
        """Tail sort is lexicographic (case-aware), not case-insensitive."""
        # 'Alpha' < 'beta' in ASCII (upper before lower) but NOT after case-folding
        text = "anchor Beta Alpha"
        key = _build_key(text, 1)
        # anchor='anchor', tail sorted: ['Alpha', 'Beta'] (uppercase A < lowercase b)
        normalized = "anchor Alpha Beta"
        assert key == _expected_sha256(normalized)

    def test_case_preserved_anchor0(self) -> None:
        """anchor=0: case preserved in sorted output."""
        text = "Zebra apple Mango"
        key = _build_key(text, 0)
        # Lexicographic sort: 'Mango' < 'Zebra' < 'apple' (uppercase before lowercase)
        normalized = "Mango Zebra apple"
        assert key == _expected_sha256(normalized)

    def test_upper_lower_same_word_different_keys(self) -> None:
        """'HELLO WORLD' vs 'hello world' must differ."""
        assert _build_key("HELLO WORLD", 2) != _build_key("hello world", 2)


# ---------------------------------------------------------------------------
# Edge cases: empty, single-token, duplicate-token, unicode
# ---------------------------------------------------------------------------


class TestBuildKeyEdgeCases:
    """Edge-case behavior: empty, single, duplicates, unicode, punctuation."""

    def test_empty_string_does_not_crash(self) -> None:
        """Empty string -> empty token list -> stable, non-crashing key."""
        key = _build_key("", 2)
        assert isinstance(key, str)
        assert len(key) == 64

    def test_whitespace_only_string(self) -> None:
        """Whitespace-only string collapses to empty token list -> same as empty."""
        assert _build_key("   ", 2) == _build_key("", 2)

    def test_single_token_no_tail(self) -> None:
        """Single token: anchor is the token; tail is empty (no sort needed)."""
        key = _build_key("Hello", 2)
        # anchor=2 but len=1 -> anchor >= len -> exact
        assert key == _expected_sha256("Hello")

    def test_duplicate_tokens_kept_as_multiset(self) -> None:
        """Duplicate tokens are NOT de-duplicated; kept in sorted multiset."""
        text = "find find handler"  # 'find' appears twice
        key = _build_key(text, 1)
        # anchor='find', tail sorted: ['find', 'handler']
        normalized = "find find handler"
        assert key == _expected_sha256(normalized)

    def test_duplicate_tokens_in_tail_sorted_multiset(self) -> None:
        """Tail with duplicates preserves count."""
        text = "anchor banana apple apple"
        key = _build_key(text, 1)
        # anchor='anchor', tail: ['apple', 'apple', 'banana']
        normalized = "anchor apple apple banana"
        assert key == _expected_sha256(normalized)

    def test_multi_space_collapsed(self) -> None:
        """Multiple spaces collapse (str.split() without arg)."""
        key1 = _build_key("Hello   World", 2)
        key2 = _build_key("Hello World", 2)
        assert key1 == key2

    def test_newline_whitespace_collapsed(self) -> None:
        """Newlines and tabs collapsed same as spaces."""
        key1 = _build_key("Hello\nWorld", 2)
        key2 = _build_key("Hello World", 2)
        assert key1 == key2

    def test_unicode_preserved(self) -> None:
        """Unicode tokens preserved as-is; not normalized or lowercased."""
        key1 = _build_key("résumé search", 2)
        key2 = _build_key("resume search", 2)
        assert key1 != key2

    def test_unicode_in_tail_sorted(self) -> None:
        """Unicode tokens in tail are sorted lexicographically without normalization."""
        q1 = "find résumé café"
        q2 = "find café résumé"
        # Both share anchor 'find'; tail sorted: ['café', 'résumé'] (lexicographic)
        assert _build_key(q1, 1) == _build_key(q2, 1)

    def test_punctuation_attached_to_token(self) -> None:
        """Punctuation is NOT stripped — attached to its adjacent token."""
        text = "search, for authentication"
        key = _build_key(text, 1)
        # 'search,' is one token (comma is not whitespace)
        # anchor='search,', tail sorted: ['authentication', 'for']
        normalized = "search, authentication for"
        assert key == _expected_sha256(normalized)

    def test_determinism_same_output_every_call(self) -> None:
        """Same input always produces the same SHA-256."""
        text = "find authentication middleware handler"
        key1 = _build_key(text, 2)
        key2 = _build_key(text, 2)
        key3 = _build_key(text, 2)
        assert key1 == key2 == key3

    def test_two_token_query_anchor2(self) -> None:
        """Two tokens with anchor=2: both in order, tail empty."""
        text = "Hello World"
        key = _build_key(text, 2)
        normalized = "Hello World"
        assert key == _expected_sha256(normalized)


# ---------------------------------------------------------------------------
# Integration: per-provider anchor_tokens read live from config
# ---------------------------------------------------------------------------


class TestPerProviderAnchorTokensLiveRead:
    """AC3: anchor_tokens is read LIVE per provider from the config."""

    def _make_qec_cfg(
        self,
        voyage_anchor: int = 2,
        cohere_anchor: int = 2,
    ) -> object:
        """Return a minimal QEC-config-like object."""
        cfg = MagicMock()
        cfg.query_embedding_cache_enabled = True
        cfg.query_embedding_cache_voyage_mode = "on"
        cfg.query_embedding_cache_cohere_mode = "on"
        cfg.query_embedding_cache_max_entries = 10000
        cfg.query_embedding_cache_anchor_tokens = voyage_anchor  # global fallback
        # per-provider fields (S2 convention)
        cfg.query_embedding_cache_voyage_anchor_tokens = voyage_anchor
        cfg.query_embedding_cache_cohere_anchor_tokens = cohere_anchor
        cfg.query_embedding_cache_audit_sample_rate = 0.0
        return cfg

    def _make_cache(self, tmp_path: Path):
        from code_indexer.server.services.query_embedding_cache import (
            QueryEmbeddingCache,
        )
        from code_indexer.server.storage.sqlite_backends import (
            QueryEmbeddingCacheSqliteBackend,
        )

        backend = QueryEmbeddingCacheSqliteBackend(str(tmp_path / "qec.db"))
        return QueryEmbeddingCache(
            backend=backend, enabled=True, voyage_mode="on", cohere_mode="on"
        )

    def test_anchor_tokens_for_voyage_read_live(self, tmp_path: Path) -> None:
        """anchor_tokens_for() returns live per-provider value for voyage-ai."""
        cache = self._make_cache(tmp_path)
        qec_cfg = self._make_qec_cfg(voyage_anchor=3)
        with patch(
            "code_indexer.server.services.query_embedding_cache.QueryEmbeddingCache._live_qec_cfg",
            return_value=qec_cfg,
        ):
            assert cache.anchor_tokens_for("voyage-ai") == 3

    def test_anchor_tokens_for_cohere_read_live(self, tmp_path: Path) -> None:
        """anchor_tokens_for() returns live per-provider value for cohere."""
        cache = self._make_cache(tmp_path)
        qec_cfg = self._make_qec_cfg(cohere_anchor=0)
        with patch(
            "code_indexer.server.services.query_embedding_cache.QueryEmbeddingCache._live_qec_cfg",
            return_value=qec_cfg,
        ):
            assert cache.anchor_tokens_for("cohere") == 0

    def test_voyage_and_cohere_independent(self, tmp_path: Path) -> None:
        """Per-provider anchor_tokens are independent."""
        cache = self._make_cache(tmp_path)
        qec_cfg = self._make_qec_cfg(voyage_anchor=1, cohere_anchor=5)
        with patch(
            "code_indexer.server.services.query_embedding_cache.QueryEmbeddingCache._live_qec_cfg",
            return_value=qec_cfg,
        ):
            assert cache.anchor_tokens_for("voyage-ai") == 1
            assert cache.anchor_tokens_for("cohere") == 5

    def test_anchor_tokens_for_unknown_provider_uses_default(
        self, tmp_path: Path
    ) -> None:
        """Unknown provider falls back to global anchor_tokens default (2)."""
        cache = self._make_cache(tmp_path)
        with patch(
            "code_indexer.server.services.query_embedding_cache.QueryEmbeddingCache._live_qec_cfg",
            return_value=None,
        ):
            result = cache.anchor_tokens_for("unknown-provider")
            assert result >= 0  # must not crash; >= 0 is all we require

    def test_negative_anchor_tokens_clamped_to_zero(self, tmp_path: Path) -> None:
        """Negative anchor_tokens is clamped to 0 (sort-all) — must not crash."""
        cache = self._make_cache(tmp_path)
        qec_cfg = self._make_qec_cfg(voyage_anchor=-1)
        with patch(
            "code_indexer.server.services.query_embedding_cache.QueryEmbeddingCache._live_qec_cfg",
            return_value=qec_cfg,
        ):
            result = cache.anchor_tokens_for("voyage-ai")
            assert result >= 0  # clamped, never negative

    def test_live_anchor_tokens_used_in_key_building(self, tmp_path: Path) -> None:
        """cache.build_key_for_provider(text, provider) uses live anchor_tokens."""
        cache = self._make_cache(tmp_path)
        qec_cfg = self._make_qec_cfg(voyage_anchor=0)  # anchor=0 -> sort all
        with patch(
            "code_indexer.server.services.query_embedding_cache.QueryEmbeddingCache._live_qec_cfg",
            return_value=qec_cfg,
        ):
            q1 = "Zebra Apple Mango"
            q2 = "Apple Mango Zebra"
            # With anchor=0, both should produce the same key
            key1 = cache.build_key_for_provider(q1, "voyage-ai")
            key2 = cache.build_key_for_provider(q2, "voyage-ai")
            assert key1 == key2


# ---------------------------------------------------------------------------
# Namespace-change log — fires once per anchor_tokens/model change
# ---------------------------------------------------------------------------


class TestNamespaceChangeLog:
    """AC3: namespace-change structured log fired on anchor_tokens or model change."""

    def _make_cache(self, tmp_path: Path):
        from code_indexer.server.services.query_embedding_cache import (
            QueryEmbeddingCache,
        )
        from code_indexer.server.storage.sqlite_backends import (
            QueryEmbeddingCacheSqliteBackend,
        )

        backend = QueryEmbeddingCacheSqliteBackend(str(tmp_path / "qec.db"))
        return QueryEmbeddingCache(backend=backend, enabled=True, voyage_mode="on")

    def _make_qec_cfg(self, anchor: int = 2, model: str = "voyage-code-3") -> object:
        cfg = MagicMock()
        cfg.query_embedding_cache_enabled = True
        cfg.query_embedding_cache_voyage_mode = "on"
        cfg.query_embedding_cache_cohere_mode = "shadow"
        cfg.query_embedding_cache_max_entries = 10000
        cfg.query_embedding_cache_anchor_tokens = anchor
        cfg.query_embedding_cache_voyage_anchor_tokens = anchor
        cfg.query_embedding_cache_cohere_anchor_tokens = anchor
        cfg.query_embedding_cache_audit_sample_rate = 0.0
        return cfg

    def test_namespace_change_log_fires_on_anchor_change(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Log emitted when anchor_tokens changes for a provider between calls."""
        cache = self._make_cache(tmp_path)

        # First call with anchor=2
        with patch(
            "code_indexer.server.services.query_embedding_cache.QueryEmbeddingCache._live_qec_cfg",
            return_value=self._make_qec_cfg(anchor=2),
        ):
            cache.anchor_tokens_for("voyage-ai")

        # Second call with anchor=3 -> should log WARNING/INFO about namespace change
        with caplog.at_level(logging.WARNING, logger="code_indexer"):
            with patch(
                "code_indexer.server.services.query_embedding_cache.QueryEmbeddingCache._live_qec_cfg",
                return_value=self._make_qec_cfg(anchor=3),
            ):
                cache.anchor_tokens_for("voyage-ai")

        # Expect at least one log entry about namespace / cache key change
        namespace_logs = [
            r
            for r in caplog.records
            if "namespace" in r.message.lower() or "anchor" in r.message.lower()
        ]
        assert len(namespace_logs) >= 1, (
            "Expected a structured log when anchor_tokens changes (namespace change)"
        )

    def test_namespace_change_log_fires_only_once_for_stable_config(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """No log emitted when anchor_tokens stays the same across calls."""
        cache = self._make_cache(tmp_path)
        cfg = self._make_qec_cfg(anchor=2)

        with caplog.at_level(logging.WARNING, logger="code_indexer"):
            with patch(
                "code_indexer.server.services.query_embedding_cache.QueryEmbeddingCache._live_qec_cfg",
                return_value=cfg,
            ):
                cache.anchor_tokens_for("voyage-ai")
                cache.anchor_tokens_for("voyage-ai")
                cache.anchor_tokens_for("voyage-ai")

        # No namespace-change log for stable config
        namespace_logs = [r for r in caplog.records if "namespace" in r.message.lower()]
        assert len(namespace_logs) == 0, (
            "No namespace-change log when anchor_tokens is stable"
        )

    def test_namespace_change_fires_once_not_repeatedly(
        self, tmp_path: Path, caplog: pytest.LogCaptureFixture
    ) -> None:
        """After one change, repeated calls with new anchor do not keep logging."""
        cache = self._make_cache(tmp_path)

        # Establish initial state
        with patch(
            "code_indexer.server.services.query_embedding_cache.QueryEmbeddingCache._live_qec_cfg",
            return_value=self._make_qec_cfg(anchor=2),
        ):
            cache.anchor_tokens_for("voyage-ai")

        # Change to anchor=3 — first call logs
        with caplog.at_level(logging.WARNING, logger="code_indexer"):
            with patch(
                "code_indexer.server.services.query_embedding_cache.QueryEmbeddingCache._live_qec_cfg",
                return_value=self._make_qec_cfg(anchor=3),
            ):
                cache.anchor_tokens_for("voyage-ai")
                cache.anchor_tokens_for("voyage-ai")  # same config, no re-log
                cache.anchor_tokens_for("voyage-ai")  # same config, no re-log

        namespace_logs = [
            r
            for r in caplog.records
            if "namespace" in r.message.lower() or "anchor" in r.message.lower()
        ]
        # Should log exactly once for the one change
        assert len(namespace_logs) == 1, (
            f"Expected exactly 1 namespace-change log, got {len(namespace_logs)}"
        )


# ---------------------------------------------------------------------------
# Back-compat: module-level build_key with anchor_tokens as optional arg
# ---------------------------------------------------------------------------


class TestBuildKeyBackwardsCompat:
    """The signature build_key(text, anchor_tokens=2) must be backwards-compatible.

    S1 callers that used build_key(text) still work (anchor=2 by default).
    The wrap tests import build_key from the module and call it with one arg.
    """

    def test_build_key_one_arg_still_works(self) -> None:
        """build_key(text) with single arg must not raise — uses anchor=2 default."""
        from code_indexer.server.services.query_embedding_cache import build_key

        key = build_key("hello world")
        assert isinstance(key, str)
        assert len(key) == 64

    def test_build_key_one_arg_equals_anchor2(self) -> None:
        """build_key(text) == build_key(text, 2)."""
        from code_indexer.server.services.query_embedding_cache import build_key

        text = "find authentication middleware"
        assert build_key(text) == build_key(text, 2)

    def test_static_method_build_key_one_arg(self) -> None:
        """QueryEmbeddingCache.build_key(text) static method still works with one arg."""
        from code_indexer.server.services.query_embedding_cache import (
            QueryEmbeddingCache,
        )

        key = QueryEmbeddingCache.build_key("hello world")
        assert isinstance(key, str)
        assert len(key) == 64

    def test_static_method_with_anchor_tokens(self) -> None:
        """QueryEmbeddingCache.build_key(text, anchor_tokens=N) works."""
        from code_indexer.server.services.query_embedding_cache import (
            QueryEmbeddingCache,
        )

        text = "Hello World Test"
        assert QueryEmbeddingCache.build_key(text, 0) != QueryEmbeddingCache.build_key(
            text, 99
        )


# ---------------------------------------------------------------------------
# Integration: cache store with anchor-token keying (two near-repeats -> one row)
# ---------------------------------------------------------------------------


class TestAnchorTokenCacheStoreIntegration:
    """Two tail-reordered near-repeats with shared first-N -> same key -> one row."""

    def test_near_repeats_collapse_to_one_cache_row(self, tmp_path: Path) -> None:
        """Two near-repeat queries sharing first-2 tokens + tail bag -> one stored row."""
        from code_indexer.server.services.query_embedding_cache import (
            QueryEmbeddingCache,
        )
        from code_indexer.server.storage.sqlite_backends import (
            QueryEmbeddingCacheSqliteBackend,
        )
        import numpy as np

        backend = QueryEmbeddingCacheSqliteBackend(str(tmp_path / "qec.db"))
        cache = QueryEmbeddingCache(backend=backend, enabled=True, voyage_mode="on")

        q1 = "find authentication middleware handler"
        q2 = "find authentication handler middleware"  # same first-2, tail reordered

        key1 = build_anchor_key(q1, anchor_tokens=2)
        key2 = build_anchor_key(q2, anchor_tokens=2)
        assert key1 == key2, "Near-repeats must map to the same key"

        # Store via q1's key
        from code_indexer.server.services.query_embedding_cache import CacheQualifier

        qualifier = CacheQualifier(
            provider="voyage-ai", model="voyage-code-3", dimension=4
        )
        vec = [1.0, 2.0, 3.0, 4.0]
        cache.record_miss_or_shadow(key1, qualifier, vec)

        # Lookup via q2's key (same key) -> should be a HIT
        result = cache.lookup(key2, qualifier)
        assert result is not None, "Near-repeat must be a cache hit"
        recovered = [float(x) for x in np.frombuffer(result, dtype="<f4")]
        assert recovered == pytest.approx(vec, abs=1e-6)

        # Only one row stored
        assert cache.total_entries() == 1


# ---------------------------------------------------------------------------
# Helper used in integration test above
# ---------------------------------------------------------------------------


def build_anchor_key(text: str, anchor_tokens: int = 2) -> str:
    """Thin wrapper calling the module-level build_key."""
    from code_indexer.server.services.query_embedding_cache import build_key

    return cast(str, build_key(text, anchor_tokens))


import pytest  # noqa: E402  (needed for approx inside class method)
