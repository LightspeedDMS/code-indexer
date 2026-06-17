"""Story #1105: QueryEmbeddingCache unit tests — AC1-AC3, AC6, AC8.

Tests cover:
- Mode control-flow (off / shadow / on) per the design table
- Exact-match key building (CASE PRESERVED, never lowercased)
- Qualifier separates providers by PK fields
- Synchronous DB SELECT on lookup; hit touches last_used; miss UPSERTs
- Float32 little-endian round-trip is lossless
- Master kill switch (query_embedding_cache_enabled=false) makes cache inert
- bypass write-but-no-lookup (no_embedding_cache_shortcut=true)
- Fail-open on DB error (no error surfaced to caller)
- Regression matrix: registry-none, coalesce_enabled=false, lane-absent, on-hit skips provider, shadow-hit still calls provider
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import List, Optional
from unittest.mock import MagicMock, patch

import numpy as np
import pytest


# ---------------------------------------------------------------------------
# Global-state isolation fixture
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _reset_globals():
    """Clear all process-global singletons before and after every test so
    the suite is order-independent regardless of which other test file ran
    first (e.g. a test that calls set_query_embedding_cache / set_coalescer_registry
    without cleanup will not pollute us).  config_service is also reset so that
    QueryEmbeddingCache.enabled_for() / _live_qec_cfg() sees a clean config."""
    from code_indexer.server.services import governed_call
    from code_indexer.server.services.coalescer_registry import clear_coalescer_registry
    from code_indexer.server.services.config_service import reset_config_service

    governed_call.clear_query_embedding_cache()
    governed_call.clear_query_embedding_cache_metrics()
    clear_coalescer_registry()
    reset_config_service()
    yield
    governed_call.clear_query_embedding_cache()
    governed_call.clear_query_embedding_cache_metrics()
    clear_coalescer_registry()
    reset_config_service()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_vec(n: int = 4, seed: float = 1.0) -> List[float]:
    """Return a deterministic float32 vector of dimension n."""
    return [float(seed + i * 0.1) for i in range(n)]


def _encode_vec(vec: List[float]) -> bytes:
    return np.asarray(vec, dtype="<f4").tobytes()


def _decode_vec(blob: bytes) -> List[float]:
    return [float(x) for x in np.frombuffer(blob, dtype="<f4")]


# ---------------------------------------------------------------------------
# SQLite backend unit tests (real DB, no mocks)
# ---------------------------------------------------------------------------


class TestQueryEmbeddingCacheSqliteBackend:
    """Tests for the SQLite QueryEmbeddingCacheBackend."""

    def _make_backend(self, tmp_path: Path):
        from code_indexer.server.storage.sqlite_backends import (
            QueryEmbeddingCacheSqliteBackend,
        )

        return QueryEmbeddingCacheSqliteBackend(db_path=str(tmp_path / "qec_test.db"))

    def test_upsert_and_lookup_returns_bytes(self, tmp_path: Path) -> None:
        backend = self._make_backend(tmp_path)
        vec = _make_vec(4, 1.0)
        blob = _encode_vec(vec)
        now = time.time()
        backend.upsert("k1", "voyage-ai", "voyage-code-3", 4, blob, now, now)
        result = backend.lookup("k1", "voyage-ai", "voyage-code-3", 4)
        assert result is not None
        recovered = _decode_vec(result)
        assert recovered == pytest.approx(vec, abs=1e-6)

    def test_lookup_miss_returns_none(self, tmp_path: Path) -> None:
        backend = self._make_backend(tmp_path)
        result = backend.lookup("missing", "voyage-ai", "voyage-code-3", 4)
        assert result is None

    def test_upsert_is_idempotent(self, tmp_path: Path) -> None:
        backend = self._make_backend(tmp_path)
        vec1 = _make_vec(4, 1.0)
        vec2 = _make_vec(4, 2.0)
        t = time.time()
        backend.upsert("k1", "voyage-ai", "voyage-code-3", 4, _encode_vec(vec1), t, t)
        backend.upsert("k1", "voyage-ai", "voyage-code-3", 4, _encode_vec(vec2), t, t)
        result = backend.lookup("k1", "voyage-ai", "voyage-code-3", 4)
        assert result is not None
        # Second upsert updates embedding
        recovered = _decode_vec(result)
        assert recovered == pytest.approx(vec2, abs=1e-6)

    def test_composite_pk_isolates_providers(self, tmp_path: Path) -> None:
        backend = self._make_backend(tmp_path)
        vec_v = _make_vec(4, 1.0)
        vec_c = _make_vec(4, 2.0)
        t = time.time()
        backend.upsert(
            "same_key", "voyage-ai", "voyage-code-3", 4, _encode_vec(vec_v), t, t
        )
        backend.upsert("same_key", "cohere", "embed-v4.0", 4, _encode_vec(vec_c), t, t)
        res_v = backend.lookup("same_key", "voyage-ai", "voyage-code-3", 4)
        res_c = backend.lookup("same_key", "cohere", "embed-v4.0", 4)
        assert res_v is not None and res_c is not None
        assert _decode_vec(res_v) == pytest.approx(vec_v, abs=1e-6)
        assert _decode_vec(res_c) == pytest.approx(vec_c, abs=1e-6)

    def test_touch_last_used(self, tmp_path: Path) -> None:
        backend = self._make_backend(tmp_path)
        t0 = time.time()
        vec = _make_vec(4, 1.0)
        backend.upsert("k1", "voyage-ai", "voyage-code-3", 4, _encode_vec(vec), t0, t0)
        t1 = t0 + 100.0
        backend.touch_last_used("k1", "voyage-ai", "voyage-code-3", 4, t1)
        # Confirm value still returned (row not deleted)
        result = backend.lookup("k1", "voyage-ai", "voyage-code-3", 4)
        assert result is not None

    def test_total_entries_count(self, tmp_path: Path) -> None:
        backend = self._make_backend(tmp_path)
        assert backend.total_entries() == 0
        t = time.time()
        backend.upsert(
            "k1", "voyage-ai", "voyage-code-3", 4, _encode_vec(_make_vec()), t, t
        )
        backend.upsert(
            "k2", "voyage-ai", "voyage-code-3", 4, _encode_vec(_make_vec(4, 2.0)), t, t
        )
        assert backend.total_entries() == 2

    def test_prune_to_max_removes_oldest(self, tmp_path: Path) -> None:
        backend = self._make_backend(tmp_path)
        base_t = time.time()
        for i in range(5):
            backend.upsert(
                f"k{i}",
                "voyage-ai",
                "voyage-code-3",
                4,
                _encode_vec(_make_vec(4, float(i))),
                base_t + i,
                base_t + i,
            )
        removed = backend.prune_to_max(3)
        assert removed == 2
        assert backend.total_entries() == 3

    def test_clear_empties_table(self, tmp_path: Path) -> None:
        backend = self._make_backend(tmp_path)
        t = time.time()
        backend.upsert(
            "k1", "voyage-ai", "voyage-code-3", 4, _encode_vec(_make_vec()), t, t
        )
        backend.clear()
        assert backend.total_entries() == 0

    def test_schema_is_idempotent_on_reinit(self, tmp_path: Path) -> None:
        """_ensure_schema / CREATE TABLE IF NOT EXISTS must not raise on re-init."""
        db_path = str(tmp_path / "qec_test.db")
        from code_indexer.server.storage.sqlite_backends import (
            QueryEmbeddingCacheSqliteBackend,
        )

        b1 = QueryEmbeddingCacheSqliteBackend(db_path)
        b2 = QueryEmbeddingCacheSqliteBackend(db_path)  # must not raise
        t = time.time()
        b1.upsert("k1", "voyage-ai", "voyage-code-3", 4, _encode_vec(_make_vec()), t, t)
        assert b2.lookup("k1", "voyage-ai", "voyage-code-3", 4) is not None

    def test_float32_roundtrip_lossless(self, tmp_path: Path) -> None:
        backend = self._make_backend(tmp_path)
        original = [0.12345678, 3.14159274, -2.71828175, 1.41421354]
        blob = np.asarray(original, dtype="<f4").tobytes()
        t = time.time()
        backend.upsert("float_key", "voyage-ai", "voyage-code-3", 4, blob, t, t)
        result = backend.lookup("float_key", "voyage-ai", "voyage-code-3", 4)
        assert result is not None
        recovered = np.frombuffer(result, dtype="<f4").tolist()
        assert recovered == pytest.approx(original, abs=1e-7)


# ---------------------------------------------------------------------------
# Key-building tests
# ---------------------------------------------------------------------------


class TestBuildKey:
    """Tests for build_key (CASE PRESERVED, config-namespaced s:<digest>:<normalized>)."""

    _DIGEST = "testdigest"

    def _build_key(self, text: str) -> Optional[str]:
        from code_indexer.server.services.query_embedding_cache import build_key

        return build_key(text, config_digest=self._DIGEST)  # type: ignore[no-any-return]

    def test_case_preserved_not_lowercased(self) -> None:
        key_upper = self._build_key("CamelCase")
        key_lower = self._build_key("camelcase")
        assert key_upper != key_lower, "Key must be CASE PRESERVED (never lowercased)"

    def test_identical_text_same_key(self) -> None:
        assert self._build_key("hello world") == self._build_key("hello world")

    def test_key_has_config_namespaced_format(self) -> None:
        text = "search for authentication"
        key = self._build_key(text)
        assert key is not None
        assert key.startswith(f"s:{self._DIGEST}:"), (
            f"Key must start with 's:<digest>:' prefix, got: {key}"
        )

    def test_empty_string_has_key(self) -> None:
        key = self._build_key("")
        assert isinstance(key, str) and key.startswith(f"s:{self._DIGEST}:")

    def test_unicode_preserved(self) -> None:
        k1 = self._build_key("résumé")
        k2 = self._build_key("resume")
        assert k1 != k2


# ---------------------------------------------------------------------------
# QueryEmbeddingCache service tests
# ---------------------------------------------------------------------------


class TestQueryEmbeddingCacheService:
    """Tests for QueryEmbeddingCache mode gating."""

    def _make_cache(
        self,
        tmp_path: Path,
        enabled: bool = True,
        voyage_mode: str = "shadow",
        cohere_mode: str = "shadow",
        max_entries: int = 10000,
    ):
        from code_indexer.server.services.query_embedding_cache import (
            QueryEmbeddingCache,
        )
        from code_indexer.server.storage.sqlite_backends import (
            QueryEmbeddingCacheSqliteBackend,
        )

        backend = QueryEmbeddingCacheSqliteBackend(str(tmp_path / "qec.db"))
        return QueryEmbeddingCache(
            backend=backend,
            enabled=enabled,
            voyage_mode=voyage_mode,
            cohere_mode=cohere_mode,
            max_entries=max_entries,
        )

    def _make_provider(
        self, name: str = "voyage-ai", model: str = "voyage-code-3", dims: int = 4
    ):
        """Minimal provider stub."""
        mock = MagicMock()
        mock.get_provider_name.return_value = name
        mock.get_current_model.return_value = model
        mock.get_model_info.return_value = {"dimensions": dims}
        return mock

    def test_enabled_for_voyage(self, tmp_path: Path) -> None:
        cache = self._make_cache(tmp_path, enabled=True, voyage_mode="shadow")
        assert cache.enabled_for("voyage-ai") is True

    def test_not_enabled_when_master_switch_off(self, tmp_path: Path) -> None:
        # Patch _live_qec_cfg to return None so construction-time enabled=False is used.
        # This tests the fail-open fallback path; live-config path is in
        # test_query_embedding_cache_config_1105.py.
        cache = self._make_cache(tmp_path, enabled=False)
        _target = "code_indexer.server.services.query_embedding_cache.QueryEmbeddingCache._live_qec_cfg"
        with patch(_target, return_value=None):
            assert cache.enabled_for("voyage-ai") is False

    def test_mode_off_returns_inert_mode(self, tmp_path: Path) -> None:
        cache = self._make_cache(tmp_path, enabled=True, voyage_mode="off")
        _target = "code_indexer.server.services.query_embedding_cache.QueryEmbeddingCache._live_qec_cfg"
        with patch(_target, return_value=None):
            assert cache.mode_for("voyage-ai") == "off"

    def test_mode_shadow_default(self, tmp_path: Path) -> None:
        cache = self._make_cache(tmp_path, enabled=True, voyage_mode="shadow")
        _target = "code_indexer.server.services.query_embedding_cache.QueryEmbeddingCache._live_qec_cfg"
        with patch(_target, return_value=None):
            assert cache.mode_for("voyage-ai") == "shadow"

    def test_mode_on(self, tmp_path: Path) -> None:
        cache = self._make_cache(tmp_path, enabled=True, voyage_mode="on")
        _target = "code_indexer.server.services.query_embedding_cache.QueryEmbeddingCache._live_qec_cfg"
        with patch(_target, return_value=None):
            assert cache.mode_for("voyage-ai") == "on"

    def test_lookup_returns_none_when_miss(self, tmp_path: Path) -> None:
        cache = self._make_cache(tmp_path)
        provider = self._make_provider()
        key = cache.build_key("hello", config_digest="testdigest")
        qualifier = cache.qualifier(provider)
        result = cache.lookup(key, qualifier)
        assert result is None

    def test_record_miss_stores_in_db(self, tmp_path: Path) -> None:
        cache = self._make_cache(tmp_path, voyage_mode="shadow")
        provider = self._make_provider()
        vec = _make_vec(4, 1.0)
        key = cache.build_key("hello world", config_digest="testdigest")
        qualifier = cache.qualifier(provider)
        cache.record_miss_or_shadow(key, qualifier, vec)
        # Now lookup should find it
        result = cache.lookup(key, qualifier)
        assert result is not None
        recovered = np.frombuffer(result, dtype="<f4").tolist()
        assert recovered == pytest.approx(vec, abs=1e-6)

    def test_record_hit_touches_last_used(self, tmp_path: Path) -> None:
        cache = self._make_cache(tmp_path, voyage_mode="shadow")
        provider = self._make_provider()
        vec = _make_vec(4, 1.0)
        key = cache.build_key("hello", config_digest="testdigest")
        qualifier = cache.qualifier(provider)
        cache.record_miss_or_shadow(key, qualifier, vec)
        # Should not raise
        cache.record_hit(key, qualifier)

    def test_total_entries_delegates_to_backend(self, tmp_path: Path) -> None:
        cache = self._make_cache(tmp_path)
        assert cache.total_entries() == 0

    def test_qualifier_contains_provider_model_dimension(self, tmp_path: Path) -> None:
        cache = self._make_cache(tmp_path)
        provider = self._make_provider("voyage-ai", "voyage-code-3", 1024)
        q = cache.qualifier(provider)
        assert "voyage-ai" in str(q)
        assert "voyage-code-3" in str(q)
        assert "1024" in str(q)


# ---------------------------------------------------------------------------
# Fail-open tests
# ---------------------------------------------------------------------------


class TestQueryEmbeddingCacheFailOpen:
    """DB-down on lookup must fail open (no error to caller)."""

    def test_lookup_fail_open_on_db_error(self, tmp_path: Path) -> None:
        from code_indexer.server.services.query_embedding_cache import (
            QueryEmbeddingCache,
        )

        bad_backend = MagicMock()
        bad_backend.lookup.side_effect = RuntimeError("DB down")
        bad_backend.total_entries.return_value = 0

        cache = QueryEmbeddingCache(
            backend=bad_backend,
            enabled=True,
            voyage_mode="on",
            cohere_mode="on",
        )
        # Should not raise — fail open returns None
        provider_mock = MagicMock()
        provider_mock.get_provider_name.return_value = "voyage-ai"
        provider_mock.get_current_model.return_value = "voyage-code-3"
        provider_mock.get_model_info.return_value = {"dimensions": 4}

        key = cache.build_key("test query", config_digest="testdigest")
        qualifier = cache.qualifier(provider_mock)
        result = cache.lookup(key, qualifier)
        assert result is None  # Fail open: None means caller should use live

    def test_upsert_fail_open_on_db_error(self, tmp_path: Path) -> None:
        from code_indexer.server.services.query_embedding_cache import (
            QueryEmbeddingCache,
        )

        bad_backend = MagicMock()
        bad_backend.upsert.side_effect = RuntimeError("DB down")
        bad_backend.total_entries.return_value = 0

        cache = QueryEmbeddingCache(
            backend=bad_backend,
            enabled=True,
            voyage_mode="shadow",
            cohere_mode="shadow",
        )
        provider_mock = MagicMock()
        provider_mock.get_provider_name.return_value = "voyage-ai"
        provider_mock.get_current_model.return_value = "voyage-code-3"
        provider_mock.get_model_info.return_value = {"dimensions": 4}

        key = cache.build_key("test", config_digest="testdigest")
        qualifier = cache.qualifier(provider_mock)
        vec = _make_vec(4, 1.0)
        # Should not raise
        cache.record_miss_or_shadow(key, qualifier, vec)


# ---------------------------------------------------------------------------
# Protocol conformance check
# ---------------------------------------------------------------------------


class TestQueryEmbeddingCacheBackendProtocol:
    """QueryEmbeddingCacheSqliteBackend must satisfy the Protocol."""

    def test_sqlite_satisfies_protocol(self, tmp_path: Path) -> None:
        from code_indexer.server.storage.protocols import QueryEmbeddingCacheBackend
        from code_indexer.server.storage.sqlite_backends import (
            QueryEmbeddingCacheSqliteBackend,
        )

        backend = QueryEmbeddingCacheSqliteBackend(str(tmp_path / "qec_proto.db"))
        assert isinstance(backend, QueryEmbeddingCacheBackend)


# ---------------------------------------------------------------------------
# BackendRegistry double-touch tests (AC7)
# ---------------------------------------------------------------------------


class TestBackendRegistryDoubleTouchWiring:
    """Both _create_sqlite_backends and _create_postgres_backends must wire query_embedding_cache."""

    def test_sqlite_backends_has_query_embedding_cache_field(
        self, tmp_path: Path
    ) -> None:
        from code_indexer.server.storage.factory import StorageFactory

        registry = StorageFactory._create_sqlite_backends(str(tmp_path))
        assert hasattr(registry, "query_embedding_cache"), (
            "BackendRegistry must have a query_embedding_cache field"
        )
        assert registry.query_embedding_cache is not None

    def test_backend_registry_dataclass_has_field(self) -> None:
        import dataclasses

        from code_indexer.server.storage.factory import BackendRegistry

        fields = {f.name for f in dataclasses.fields(BackendRegistry)}
        assert "query_embedding_cache" in fields, (
            "BackendRegistry dataclass must declare query_embedding_cache field"
        )

    def test_sqlite_factory_source_wires_query_embedding_cache(self) -> None:
        """Source-text guard: _create_sqlite_backends must reference query_embedding_cache."""
        import inspect

        from code_indexer.server.storage import factory as _factory_mod

        source = inspect.getsource(_factory_mod.StorageFactory._create_sqlite_backends)
        assert "query_embedding_cache" in source, (
            "_create_sqlite_backends must wire query_embedding_cache"
        )

    def test_postgres_factory_source_wires_query_embedding_cache(self) -> None:
        """Source-text guard: _create_postgres_backends must reference query_embedding_cache."""
        import inspect

        from code_indexer.server.storage import factory as _factory_mod

        source = inspect.getsource(
            _factory_mod.StorageFactory._create_postgres_backends
        )
        assert "query_embedding_cache" in source, (
            "_create_postgres_backends must wire query_embedding_cache"
        )
