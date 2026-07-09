"""Regression tests for Messi Rule #12 orphan-knob removal (Epic #1103).

Two global fields were dead/unwired:
- query_embedding_cache_anchor_tokens  (global fallback, not on UI)
- query_embedding_cache_audit_sample_rate  (global, zero consumers)

Fix: remove both fields from QueryEmbeddingCacheConfig; replace the global
fallback in anchor_tokens_for() with the module-level DEFAULT_ANCHOR_TOKENS=2
constant.

Guards:
  R1  anchor_tokens_for() returns DEFAULT_ANCHOR_TOKENS (2) when per-provider
      value is None and no global config field exists on the config object.
  R2  anchor_tokens_for() still returns the per-provider value when set.
  R3  Loading a QueryEmbeddingCacheConfig from a dict that contains the two
      removed keys does NOT raise (backward-compat: unknown keys ignored).
  R4  query_embedding_cache_voyage_anchor_tokens still declared on dataclass.
  R5  query_embedding_cache_cohere_anchor_tokens still declared on dataclass.
  R6  query_embedding_cache_voyage_audit_sample_rate still declared on dataclass.
  R7  query_embedding_cache_cohere_audit_sample_rate still declared on dataclass.
  R8  query_embedding_cache_anchor_tokens is ABSENT from the dataclass.
  R9  query_embedding_cache_audit_sample_rate is ABSENT from the dataclass.
"""

from __future__ import annotations

import dataclasses
from pathlib import Path
from unittest.mock import patch


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _get_qec_fields() -> set[str]:
    from code_indexer.server.utils.config_manager import QueryEmbeddingCacheConfig

    return {f.name for f in dataclasses.fields(QueryEmbeddingCacheConfig)}


def _make_cache(tmp_path: Path):
    from code_indexer.server.services.query_embedding_cache import QueryEmbeddingCache
    from code_indexer.server.storage.sqlite_backends import (
        QueryEmbeddingCacheSqliteBackend,
    )

    backend = QueryEmbeddingCacheSqliteBackend(str(tmp_path / "qec.db"))
    return QueryEmbeddingCache(
        backend=backend, enabled=True, voyage_mode="on", cohere_mode="on"
    )


def _make_qec_cfg_no_global(
    voyage_anchor: int | None = None,
    cohere_anchor: int | None = None,
) -> object:
    """Return a real QueryEmbeddingCacheConfig WITHOUT the removed global fields."""
    from code_indexer.server.utils.config_manager import QueryEmbeddingCacheConfig

    kwargs: dict = {}
    if voyage_anchor is not None:
        kwargs["query_embedding_cache_voyage_anchor_tokens"] = voyage_anchor
    if cohere_anchor is not None:
        kwargs["query_embedding_cache_cohere_anchor_tokens"] = cohere_anchor
    return QueryEmbeddingCacheConfig(**kwargs)


# ---------------------------------------------------------------------------
# R1 — fallback to DEFAULT_ANCHOR_TOKENS when per-provider is None
# ---------------------------------------------------------------------------


class TestAnchorTokensFallbackToConstant:
    def test_voyage_none_returns_default(self, tmp_path: Path) -> None:
        """R1: anchor_tokens_for('voyage-ai') returns 2 when per-provider is None."""
        cache = _make_cache(tmp_path)
        qec_cfg = _make_qec_cfg_no_global(voyage_anchor=None)
        with patch(
            "code_indexer.server.services.query_embedding_cache.QueryEmbeddingCache._live_qec_cfg",
            return_value=qec_cfg,
        ):
            result = cache.anchor_tokens_for("voyage-ai")
        assert result == 2, (
            f"Expected DEFAULT_ANCHOR_TOKENS=2 when per-provider is None, got {result}"
        )

    def test_cohere_none_returns_default(self, tmp_path: Path) -> None:
        """R1: anchor_tokens_for('cohere') returns 2 when per-provider is None."""
        cache = _make_cache(tmp_path)
        qec_cfg = _make_qec_cfg_no_global(cohere_anchor=None)
        with patch(
            "code_indexer.server.services.query_embedding_cache.QueryEmbeddingCache._live_qec_cfg",
            return_value=qec_cfg,
        ):
            result = cache.anchor_tokens_for("cohere")
        assert result == 2, (
            f"Expected DEFAULT_ANCHOR_TOKENS=2 when per-provider is None, got {result}"
        )

    def test_unknown_provider_returns_default(self, tmp_path: Path) -> None:
        """R1: anchor_tokens_for for an unknown provider name returns 2."""
        cache = _make_cache(tmp_path)
        qec_cfg = _make_qec_cfg_no_global()
        with patch(
            "code_indexer.server.services.query_embedding_cache.QueryEmbeddingCache._live_qec_cfg",
            return_value=qec_cfg,
        ):
            result = cache.anchor_tokens_for("some-other-provider")
        assert result == 2


# ---------------------------------------------------------------------------
# R2 — per-provider value still honoured when set
# ---------------------------------------------------------------------------


class TestAnchorTokensPerProviderStillWorks:
    def test_voyage_per_provider_used(self, tmp_path: Path) -> None:
        """R2: per-provider voyage value is returned when set."""
        cache = _make_cache(tmp_path)
        qec_cfg = _make_qec_cfg_no_global(voyage_anchor=5)
        with patch(
            "code_indexer.server.services.query_embedding_cache.QueryEmbeddingCache._live_qec_cfg",
            return_value=qec_cfg,
        ):
            assert cache.anchor_tokens_for("voyage-ai") == 5

    def test_cohere_per_provider_used(self, tmp_path: Path) -> None:
        """R2: per-provider cohere value is returned when set."""
        cache = _make_cache(tmp_path)
        qec_cfg = _make_qec_cfg_no_global(cohere_anchor=0)
        with patch(
            "code_indexer.server.services.query_embedding_cache.QueryEmbeddingCache._live_qec_cfg",
            return_value=qec_cfg,
        ):
            assert cache.anchor_tokens_for("cohere") == 0


# ---------------------------------------------------------------------------
# R3 — backward-compat: stale config.json with removed keys must not raise
# ---------------------------------------------------------------------------


class TestBackwardCompatLoadIgnoresRemovedKeys:
    def test_load_dict_with_anchor_tokens_does_not_raise(self) -> None:
        """R3a: constructing QueryEmbeddingCacheConfig ignores removed anchor key."""
        from code_indexer.server.utils.config_manager import QueryEmbeddingCacheConfig

        fields = {f.name for f in dataclasses.fields(QueryEmbeddingCacheConfig)}
        stale_dict = {
            "query_embedding_cache_anchor_tokens": 5,
            "query_embedding_cache_audit_sample_rate": 0.1,
        }
        # The load filter pattern used in config_manager uses fields() — simulate it:
        filtered = {k: v for k, v in stale_dict.items() if k in fields}
        # filtered must be empty (both keys removed) and construction must succeed
        cfg = QueryEmbeddingCacheConfig(**filtered)
        assert cfg is not None

    def test_load_dict_with_both_removed_keys_filtered_correctly(self) -> None:
        """R3b: the load-filter correctly discards both removed keys."""
        from code_indexer.server.utils.config_manager import QueryEmbeddingCacheConfig

        fields = {f.name for f in dataclasses.fields(QueryEmbeddingCacheConfig)}
        stale = {
            "query_embedding_cache_anchor_tokens": 3,
            "query_embedding_cache_audit_sample_rate": 0.5,
            "query_embedding_cache_voyage_anchor_tokens": 4,
        }
        filtered = {k: v for k, v in stale.items() if k in fields}
        # Only the per-provider key survives
        assert "query_embedding_cache_anchor_tokens" not in filtered
        assert "query_embedding_cache_audit_sample_rate" not in filtered
        assert filtered.get("query_embedding_cache_voyage_anchor_tokens") == 4

        cfg = QueryEmbeddingCacheConfig(**filtered)
        assert cfg.query_embedding_cache_voyage_anchor_tokens == 4


# ---------------------------------------------------------------------------
# R4–R7 — per-provider knobs still present
# ---------------------------------------------------------------------------


class TestPerProviderKnobsUnaffected:
    def test_voyage_anchor_tokens_field_present(self) -> None:
        """R4: query_embedding_cache_voyage_anchor_tokens still declared."""
        assert "query_embedding_cache_voyage_anchor_tokens" in _get_qec_fields()

    def test_cohere_anchor_tokens_field_present(self) -> None:
        """R5: query_embedding_cache_cohere_anchor_tokens still declared."""
        assert "query_embedding_cache_cohere_anchor_tokens" in _get_qec_fields()

    def test_voyage_audit_sample_rate_field_present(self) -> None:
        """R6: query_embedding_cache_voyage_audit_sample_rate still declared."""
        assert "query_embedding_cache_voyage_audit_sample_rate" in _get_qec_fields()

    def test_cohere_audit_sample_rate_field_present(self) -> None:
        """R7: query_embedding_cache_cohere_audit_sample_rate still declared."""
        assert "query_embedding_cache_cohere_audit_sample_rate" in _get_qec_fields()


# ---------------------------------------------------------------------------
# R8–R9 — removed global fields are ABSENT from the dataclass
# ---------------------------------------------------------------------------


class TestRemovedGlobalFieldsAbsent:
    def test_global_anchor_tokens_absent(self) -> None:
        """R8: query_embedding_cache_anchor_tokens must NOT be in the dataclass."""
        assert "query_embedding_cache_anchor_tokens" not in _get_qec_fields(), (
            "query_embedding_cache_anchor_tokens is an orphan knob and must be removed"
        )

    def test_global_audit_sample_rate_absent(self) -> None:
        """R9: query_embedding_cache_audit_sample_rate must NOT be in the dataclass."""
        assert "query_embedding_cache_audit_sample_rate" not in _get_qec_fields(), (
            "query_embedding_cache_audit_sample_rate is an orphan knob and must be removed"
        )
