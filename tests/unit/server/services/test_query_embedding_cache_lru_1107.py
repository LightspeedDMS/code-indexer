"""Story #1107 S3: Shared LRU count cap + deterministic eviction + Web UI config section.

Tests cover:
- prune_to_max evicts exactly down to the cap (oldest-by-last_used first)
- Deterministic tie-breaking via secondary sort columns
- Cap is SHARED across both providers (one bucket, not per-provider)
- max_entries < 100 falls back to safe default (100)
- MRU bump: recently-hit row survives eviction over older row
- Concurrent pruners converge deterministically — no crash, store ends bounded
- Config section: all 8 settings round-trip via config service
- Config validation: rejects bad mode/float/int values
- per-provider audit_sample_rate fields in config
"""

from __future__ import annotations

import threading
from pathlib import Path
from typing import List, Optional

import numpy as np
import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MIN_ENTRIES_FLOOR = 100  # minimum cap enforced by prune_to_max
_MIN_ENTRIES_SAFE_DEFAULT = 100  # safe default when below floor


def _make_vec(n: int = 4, seed: float = 1.0) -> List[float]:
    return [float(seed + i * 0.1) for i in range(n)]


def _encode_vec(vec: List[float]) -> bytes:
    return np.asarray(vec, dtype="<f4").tobytes()


def _make_backend(tmp_path: Path):
    from code_indexer.server.storage.sqlite_backends import (
        QueryEmbeddingCacheSqliteBackend,
    )

    return QueryEmbeddingCacheSqliteBackend(db_path=str(tmp_path / "qec_test.db"))


def _insert_row(
    backend, key: str, provider: str, last_used: float, created_at: float
) -> None:
    blob = _encode_vec(_make_vec(4, 1.0))
    backend.upsert(key, provider, "test-model", 4, blob, created_at, last_used)


# ---------------------------------------------------------------------------
# AC1: deterministic eviction (SQLite)
# ---------------------------------------------------------------------------


class TestPruneToMaxSqliteExact:
    """SQLite prune_to_max uses the deterministic rowid-based SQL."""

    def test_prune_evicts_down_to_cap(self, tmp_path: Path) -> None:
        """Inserting 5 rows then pruning to 3 leaves exactly 3."""
        backend = _make_backend(tmp_path)
        base_t = 1000.0
        for i in range(5):
            _insert_row(backend, f"key{i}", "voyage-ai", base_t + i, base_t + i)
        assert backend.total_entries() == 5

        deleted = backend.prune_to_max(3)
        assert deleted == 2
        assert backend.total_entries() == 3

    def test_prune_removes_oldest_by_last_used(self, tmp_path: Path) -> None:
        """The two oldest-by-last_used rows are evicted, newest survive."""
        backend = _make_backend(tmp_path)
        t = 1000.0
        # Insert 5 rows with distinct last_used values
        keys_by_age = [f"key{i}" for i in range(5)]  # key0 oldest, key4 newest
        for i, key in enumerate(keys_by_age):
            _insert_row(backend, key, "voyage-ai", t + i, t + i)

        backend.prune_to_max(3)

        # key0 and key1 (oldest) must be gone
        assert backend.lookup("key0", "voyage-ai", "test-model", 4) is None
        assert backend.lookup("key1", "voyage-ai", "test-model", 4) is None
        # key2, key3, key4 survive
        assert backend.lookup("key2", "voyage-ai", "test-model", 4) is not None
        assert backend.lookup("key3", "voyage-ai", "test-model", 4) is not None
        assert backend.lookup("key4", "voyage-ai", "test-model", 4) is not None

    def test_prune_no_op_when_below_cap(self, tmp_path: Path) -> None:
        """prune_to_max(10) with only 5 rows deletes 0."""
        backend = _make_backend(tmp_path)
        for i in range(5):
            _insert_row(backend, f"key{i}", "voyage-ai", 1000.0 + i, 1000.0 + i)

        deleted = backend.prune_to_max(10)
        assert deleted == 0
        assert backend.total_entries() == 5

    def test_prune_exactly_at_cap_deletes_zero(self, tmp_path: Path) -> None:
        """prune_to_max(N) with exactly N rows deletes 0."""
        backend = _make_backend(tmp_path)
        for i in range(4):
            _insert_row(backend, f"key{i}", "voyage-ai", 1000.0 + i, 1000.0 + i)

        deleted = backend.prune_to_max(4)
        assert deleted == 0
        assert backend.total_entries() == 4

    def test_prune_deterministic_tie_break_last_used(self, tmp_path: Path) -> None:
        """When last_used is identical, secondary sort (created_at) breaks ties."""
        backend = _make_backend(tmp_path)
        same_ts = 1000.0

        # All rows have same last_used; differ only in created_at
        # created_at increases: key0 oldest, key4 newest
        for i in range(5):
            blob = _encode_vec(_make_vec(4, float(i + 1)))
            backend.upsert(
                f"key{i}", "voyage-ai", "test-model", 4, blob, same_ts + i, same_ts
            )

        backend.prune_to_max(3)
        assert backend.total_entries() == 3

        # key0 and key1 (smallest created_at) should be evicted
        assert backend.lookup("key0", "voyage-ai", "test-model", 4) is None
        assert backend.lookup("key1", "voyage-ai", "test-model", 4) is None
        # key2, key3, key4 survive
        assert backend.lookup("key2", "voyage-ai", "test-model", 4) is not None
        assert backend.lookup("key3", "voyage-ai", "test-model", 4) is not None
        assert backend.lookup("key4", "voyage-ai", "test-model", 4) is not None


# ---------------------------------------------------------------------------
# AC1: min_entries floor (SQLite)
# ---------------------------------------------------------------------------


class TestPruneToMaxMinEntriesFloor:
    """max_entries < 100 falls back to safe default (100)."""

    def test_max_entries_below_100_uses_safe_default(self, tmp_path: Path) -> None:
        """Passing max_entries=5 (< 100) should behave as max_entries=100."""
        backend = _make_backend(tmp_path)
        # Insert 150 rows
        for i in range(150):
            _insert_row(backend, f"key{i}", "voyage-ai", 1000.0 + i, 1000.0 + i)
        assert backend.total_entries() == 150

        # max_entries=5 is below the floor of 100 — should fall back to 100
        backend.prune_to_max(5)
        # Must end up at exactly 100 (not 5)
        assert backend.total_entries() == 100

    def test_max_entries_zero_uses_safe_default(self, tmp_path: Path) -> None:
        """Passing max_entries=0 should use the safe default of 100."""
        backend = _make_backend(tmp_path)
        for i in range(150):
            _insert_row(backend, f"key{i}", "voyage-ai", 1000.0 + i, 1000.0 + i)

        backend.prune_to_max(0)
        assert backend.total_entries() == 100

    def test_max_entries_99_uses_safe_default(self, tmp_path: Path) -> None:
        """max_entries=99 (one below floor) falls back to 100."""
        backend = _make_backend(tmp_path)
        for i in range(150):
            _insert_row(backend, f"key{i}", "voyage-ai", 1000.0 + i, 1000.0 + i)

        backend.prune_to_max(99)
        assert backend.total_entries() == 100

    def test_max_entries_100_is_valid(self, tmp_path: Path) -> None:
        """max_entries=100 is the minimum valid cap — must be respected exactly."""
        backend = _make_backend(tmp_path)
        for i in range(150):
            _insert_row(backend, f"key{i}", "voyage-ai", 1000.0 + i, 1000.0 + i)

        backend.prune_to_max(100)
        assert backend.total_entries() == 100


# ---------------------------------------------------------------------------
# AC1: shared cap across both providers
# ---------------------------------------------------------------------------


class TestPruneSharedCapAcrossProviders:
    """Cap is ONE shared bucket across voyage-ai and cohere."""

    def test_shared_cap_counts_both_providers(self, tmp_path: Path) -> None:
        """With 6 voyage + 4 cohere rows, prune_to_max(5) leaves 5 total."""
        backend = _make_backend(tmp_path)
        t = 1000.0
        # Insert 6 voyage rows (older timestamps)
        for i in range(6):
            _insert_row(backend, f"voyage_key{i}", "voyage-ai", t + i, t + i)
        # Insert 4 cohere rows (newer timestamps)
        for i in range(4):
            _insert_row(backend, f"cohere_key{i}", "cohere", t + 10 + i, t + 10 + i)

        assert backend.total_entries() == 10

        backend.prune_to_max(5)
        assert backend.total_entries() == 5

    def test_eviction_by_global_recency_not_per_provider(self, tmp_path: Path) -> None:
        """Global recency: old voyage rows are evicted before newer cohere rows."""
        backend = _make_backend(tmp_path)
        t = 1000.0
        # 3 voyage rows with old last_used
        for i in range(3):
            _insert_row(backend, f"voyage_key{i}", "voyage-ai", t + i, t + i)
        # 3 cohere rows with newer last_used
        for i in range(3):
            _insert_row(backend, f"cohere_key{i}", "cohere", t + 10 + i, t + 10 + i)

        # Prune to 4: oldest 2 (voyage_key0, voyage_key1) should go
        backend.prune_to_max(4)
        assert backend.total_entries() == 4

        assert backend.lookup("voyage_key0", "voyage-ai", "test-model", 4) is None
        assert backend.lookup("voyage_key1", "voyage-ai", "test-model", 4) is None
        assert backend.lookup("voyage_key2", "voyage-ai", "test-model", 4) is not None
        # All cohere rows survive
        for i in range(3):
            assert (
                backend.lookup(f"cohere_key{i}", "cohere", "test-model", 4) is not None
            )


# ---------------------------------------------------------------------------
# AC2: MRU bump survives eviction
# ---------------------------------------------------------------------------


class TestMruBumpSurvivesEviction:
    """touch_last_used bumps a row to survive the next prune."""

    def test_mru_bump_saves_row_from_eviction(self, tmp_path: Path) -> None:
        """A hit on the oldest row bumps its last_used so it survives prune."""
        backend = _make_backend(tmp_path)
        t = 1000.0

        # Insert 5 rows: key0 oldest, key4 newest
        for i in range(5):
            _insert_row(backend, f"key{i}", "voyage-ai", t + i, t + i)

        # Simulate a HIT on key0 (the oldest) — bump its last_used to "now"
        backend.touch_last_used("key0", "voyage-ai", "test-model", 4, t + 100)

        # Prune to 4: should evict key1 (now the oldest) not key0
        backend.prune_to_max(4)
        assert backend.total_entries() == 4

        assert backend.lookup("key0", "voyage-ai", "test-model", 4) is not None
        assert backend.lookup("key1", "voyage-ai", "test-model", 4) is None


# ---------------------------------------------------------------------------
# Concurrency: two pruners running at once
# ---------------------------------------------------------------------------


class TestConcurrentPruners:
    """Two pruners running simultaneously converge deterministically."""

    def test_two_pruners_no_crash(self, tmp_path: Path) -> None:
        """Two concurrent prune_to_max calls must not crash."""
        backend = _make_backend(tmp_path)
        for i in range(200):
            _insert_row(backend, f"key{i}", "voyage-ai", 1000.0 + i, 1000.0 + i)

        errors: List[Exception] = []

        def prune():
            try:
                backend.prune_to_max(100)
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=prune)
        t2 = threading.Thread(target=prune)
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        assert not errors, f"Concurrent prune raised: {errors}"
        # Store must end up bounded (at or below cap; soft cap tolerates 1 overshoot)
        assert backend.total_entries() <= 100

    def test_two_pruners_deterministic_result(self, tmp_path: Path) -> None:
        """After two concurrent prunes both converge to the same bounded state."""
        backend = _make_backend(tmp_path)
        for i in range(50):
            _insert_row(backend, f"key{i}", "voyage-ai", 1000.0 + i, 1000.0 + i)

        errors: List[Exception] = []

        def prune():
            try:
                backend.prune_to_max(20)
            except Exception as e:
                errors.append(e)

        t1 = threading.Thread(target=prune)
        t2 = threading.Thread(target=prune)
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        assert not errors
        assert backend.total_entries() <= 20


# ---------------------------------------------------------------------------
# Config service: query_embedding_cache section (8 settings)
# ---------------------------------------------------------------------------


class TestQueryEmbeddingCacheConfigSection:
    """Config service exposes and updates all 8 query_embedding_cache settings."""

    def _make_config_service(self, tmp_path: Path):
        """Create a minimal ConfigService backed by a temp dir."""
        from code_indexer.server.services.config_service import ConfigService
        from code_indexer.server.utils.config_manager import ServerConfigManager

        server_dir = tmp_path / "server"
        server_dir.mkdir(parents=True, exist_ok=True)
        mgr = ServerConfigManager(str(server_dir))
        return ConfigService(config_manager=mgr)

    def test_get_all_settings_includes_query_embedding_cache(
        self, tmp_path: Path
    ) -> None:
        """get_all_settings() must include a 'query_embedding_cache' key."""
        svc = self._make_config_service(tmp_path)
        settings = svc.get_all_settings()
        assert "query_embedding_cache" in settings

    def test_query_embedding_cache_section_has_8_keys(self, tmp_path: Path) -> None:
        """The section must expose exactly the 8 required settings."""
        svc = self._make_config_service(tmp_path)
        section = svc.get_all_settings()["query_embedding_cache"]
        required = {
            "query_embedding_cache_enabled",
            "query_embedding_cache_max_entries",
            "query_embedding_cache_voyage_mode",
            "query_embedding_cache_voyage_anchor_tokens",
            "query_embedding_cache_voyage_audit_sample_rate",
            "query_embedding_cache_cohere_mode",
            "query_embedding_cache_cohere_anchor_tokens",
            "query_embedding_cache_cohere_audit_sample_rate",
        }
        assert required.issubset(set(section.keys()))

    def test_update_enabled_bool(self, tmp_path: Path) -> None:
        """update_setting('query_embedding_cache', 'query_embedding_cache_enabled', 'false') works."""
        svc = self._make_config_service(tmp_path)
        svc.update_setting(
            "query_embedding_cache", "query_embedding_cache_enabled", "false"
        )
        cfg = svc.get_config().query_embedding_cache_config
        assert cfg is not None
        assert cfg.query_embedding_cache_enabled is False

    def test_update_max_entries_int(self, tmp_path: Path) -> None:
        """update_setting updates max_entries as int."""
        svc = self._make_config_service(tmp_path)
        svc.update_setting(
            "query_embedding_cache", "query_embedding_cache_max_entries", "500"
        )
        cfg = svc.get_config().query_embedding_cache_config
        assert cfg is not None
        assert cfg.query_embedding_cache_max_entries == 500

    def test_update_voyage_mode(self, tmp_path: Path) -> None:
        """update_setting sets voyage mode to 'on'."""
        svc = self._make_config_service(tmp_path)
        svc.update_setting(
            "query_embedding_cache", "query_embedding_cache_voyage_mode", "on"
        )
        cfg = svc.get_config().query_embedding_cache_config
        assert cfg is not None
        assert cfg.query_embedding_cache_voyage_mode == "on"

    def test_update_cohere_mode(self, tmp_path: Path) -> None:
        """update_setting sets cohere mode to 'off'."""
        svc = self._make_config_service(tmp_path)
        svc.update_setting(
            "query_embedding_cache", "query_embedding_cache_cohere_mode", "off"
        )
        cfg = svc.get_config().query_embedding_cache_config
        assert cfg is not None
        assert cfg.query_embedding_cache_cohere_mode == "off"

    def test_update_voyage_anchor_tokens(self, tmp_path: Path) -> None:
        """update_setting sets voyage anchor_tokens."""
        svc = self._make_config_service(tmp_path)
        svc.update_setting(
            "query_embedding_cache",
            "query_embedding_cache_voyage_anchor_tokens",
            "5",
        )
        cfg = svc.get_config().query_embedding_cache_config
        assert cfg is not None
        assert cfg.query_embedding_cache_voyage_anchor_tokens == 5

    def test_update_cohere_anchor_tokens(self, tmp_path: Path) -> None:
        """update_setting sets cohere anchor_tokens."""
        svc = self._make_config_service(tmp_path)
        svc.update_setting(
            "query_embedding_cache",
            "query_embedding_cache_cohere_anchor_tokens",
            "0",
        )
        cfg = svc.get_config().query_embedding_cache_config
        assert cfg is not None
        assert cfg.query_embedding_cache_cohere_anchor_tokens == 0

    def test_update_voyage_audit_sample_rate(self, tmp_path: Path) -> None:
        """update_setting sets voyage audit_sample_rate float."""
        svc = self._make_config_service(tmp_path)
        svc.update_setting(
            "query_embedding_cache",
            "query_embedding_cache_voyage_audit_sample_rate",
            "0.1",
        )
        cfg = svc.get_config().query_embedding_cache_config
        assert cfg is not None
        assert cfg.query_embedding_cache_voyage_audit_sample_rate == pytest.approx(0.1)

    def test_update_cohere_audit_sample_rate(self, tmp_path: Path) -> None:
        """update_setting sets cohere audit_sample_rate float."""
        svc = self._make_config_service(tmp_path)
        svc.update_setting(
            "query_embedding_cache",
            "query_embedding_cache_cohere_audit_sample_rate",
            "0.5",
        )
        cfg = svc.get_config().query_embedding_cache_config
        assert cfg is not None
        assert cfg.query_embedding_cache_cohere_audit_sample_rate == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# Config validation: routes._validate_config_section
# ---------------------------------------------------------------------------


class TestQueryEmbeddingCacheValidation:
    """_validate_config_section rejects bad values for all 8 fields."""

    def _validate(self, data: dict) -> Optional[str]:
        from code_indexer.server.web.routes import _validate_config_section

        result: Optional[str] = _validate_config_section("query_embedding_cache", data)
        return result

    def test_valid_all_fields_accepted(self) -> None:
        data = {
            "query_embedding_cache_enabled": "true",
            "query_embedding_cache_max_entries": "10000",
            "query_embedding_cache_voyage_mode": "on",
            "query_embedding_cache_voyage_anchor_tokens": "2",
            "query_embedding_cache_voyage_audit_sample_rate": "0.1",
            "query_embedding_cache_cohere_mode": "shadow",
            "query_embedding_cache_cohere_anchor_tokens": "0",
            "query_embedding_cache_cohere_audit_sample_rate": "0.0",
        }
        assert self._validate(data) is None

    def test_invalid_voyage_mode_rejected(self) -> None:
        err = self._validate({"query_embedding_cache_voyage_mode": "invalid"})
        assert err is not None
        assert "mode" in err.lower() or "off" in err.lower() or "shadow" in err.lower()

    def test_invalid_cohere_mode_rejected(self) -> None:
        err = self._validate({"query_embedding_cache_cohere_mode": "FULL"})
        assert err is not None

    def test_audit_sample_rate_above_1_rejected(self) -> None:
        err = self._validate({"query_embedding_cache_voyage_audit_sample_rate": "1.1"})
        assert err is not None

    def test_audit_sample_rate_negative_rejected(self) -> None:
        err = self._validate({"query_embedding_cache_cohere_audit_sample_rate": "-0.1"})
        assert err is not None

    def test_audit_sample_rate_boundary_0_accepted(self) -> None:
        assert (
            self._validate({"query_embedding_cache_voyage_audit_sample_rate": "0.0"})
            is None
        )

    def test_audit_sample_rate_boundary_1_accepted(self) -> None:
        assert (
            self._validate({"query_embedding_cache_cohere_audit_sample_rate": "1.0"})
            is None
        )

    def test_max_entries_below_100_rejected(self) -> None:
        err = self._validate({"query_embedding_cache_max_entries": "50"})
        assert err is not None

    def test_max_entries_100_accepted(self) -> None:
        assert self._validate({"query_embedding_cache_max_entries": "100"}) is None

    def test_max_entries_non_int_rejected(self) -> None:
        err = self._validate({"query_embedding_cache_max_entries": "abc"})
        assert err is not None

    def test_negative_anchor_tokens_rejected(self) -> None:
        err = self._validate({"query_embedding_cache_voyage_anchor_tokens": "-1"})
        assert err is not None

    def test_zero_anchor_tokens_accepted(self) -> None:
        assert (
            self._validate({"query_embedding_cache_voyage_anchor_tokens": "0"}) is None
        )

    def test_cohere_negative_anchor_tokens_rejected(self) -> None:
        err = self._validate({"query_embedding_cache_cohere_anchor_tokens": "-5"})
        assert err is not None


# ---------------------------------------------------------------------------
# Config model: per-provider audit_sample_rate fields exist
# ---------------------------------------------------------------------------


class TestQueryEmbeddingCacheConfigFields:
    """QueryEmbeddingCacheConfig must have per-provider audit_sample_rate fields."""

    def test_voyage_audit_sample_rate_field_exists(self) -> None:
        from code_indexer.server.utils.config_manager import QueryEmbeddingCacheConfig

        cfg = QueryEmbeddingCacheConfig()
        assert hasattr(cfg, "query_embedding_cache_voyage_audit_sample_rate")
        assert isinstance(cfg.query_embedding_cache_voyage_audit_sample_rate, float)

    def test_cohere_audit_sample_rate_field_exists(self) -> None:
        from code_indexer.server.utils.config_manager import QueryEmbeddingCacheConfig

        cfg = QueryEmbeddingCacheConfig()
        assert hasattr(cfg, "query_embedding_cache_cohere_audit_sample_rate")
        assert isinstance(cfg.query_embedding_cache_cohere_audit_sample_rate, float)

    def test_default_audit_sample_rates_are_zero(self) -> None:
        from code_indexer.server.utils.config_manager import QueryEmbeddingCacheConfig

        cfg = QueryEmbeddingCacheConfig()
        assert cfg.query_embedding_cache_voyage_audit_sample_rate == 0.0
        assert cfg.query_embedding_cache_cohere_audit_sample_rate == 0.0


# ---------------------------------------------------------------------------
# routes.py: query_embedding_cache in _VALID_CONFIG_SECTIONS
# ---------------------------------------------------------------------------


class TestQueryEmbeddingCacheInValidSections:
    """query_embedding_cache must be registered in _VALID_CONFIG_SECTIONS."""

    def test_section_registered(self) -> None:
        from code_indexer.server.web.routes import _VALID_CONFIG_SECTIONS

        assert "query_embedding_cache" in _VALID_CONFIG_SECTIONS
