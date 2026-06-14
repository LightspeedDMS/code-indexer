"""Story #1105 — QueryEmbeddingCacheConfig model + LIVE config read tests.

Proves:
- QueryEmbeddingCacheConfig exists on ServerConfig with correct fields/defaults.
- QueryEmbeddingCache.enabled_for() reads LIVE from the config service on
  every call — flipping the config WITHOUT reconstructing the service changes
  behavior immediately (AC3 "kill switch + per-provider mode read LIVE").
- QueryEmbeddingCache.mode_for() reads LIVE — changing voyage_mode in the live
  config reflects immediately without a restart.
- Fail-open: when the config service is unavailable, construction-time defaults
  are used (no exception surfaced to the caller).
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Optional
from unittest.mock import MagicMock, patch


# ---------------------------------------------------------------------------
# Helper: minimal QueryEmbeddingCacheConfig-like object
# ---------------------------------------------------------------------------


def _make_qec_cfg(
    enabled: bool = True,
    voyage_mode: str = "shadow",
    cohere_mode: str = "shadow",
    max_entries: int = 10000,
    anchor_tokens: int = 2,
    audit_sample_rate: float = 0.0,
) -> object:
    """Return a real QueryEmbeddingCacheConfig with the given values."""
    from code_indexer.server.utils.config_manager import QueryEmbeddingCacheConfig

    return QueryEmbeddingCacheConfig(
        query_embedding_cache_enabled=enabled,
        query_embedding_cache_voyage_mode=voyage_mode,
        query_embedding_cache_cohere_mode=cohere_mode,
        query_embedding_cache_max_entries=max_entries,
        query_embedding_cache_anchor_tokens=anchor_tokens,
        query_embedding_cache_audit_sample_rate=audit_sample_rate,
    )


def _make_live_cfg(qec_cfg: Optional[object]) -> Any:
    """Return a minimal ServerConfig-like object exposing query_embedding_cache_config."""
    cfg = MagicMock()
    cfg.query_embedding_cache_config = qec_cfg
    return cfg


# ---------------------------------------------------------------------------
# TestQueryEmbeddingCacheConfigModel
# ---------------------------------------------------------------------------


class TestQueryEmbeddingCacheConfigModel:
    """QueryEmbeddingCacheConfig dataclass field and default tests."""

    def test_dataclass_exists(self) -> None:
        from code_indexer.server.utils.config_manager import QueryEmbeddingCacheConfig

        assert QueryEmbeddingCacheConfig is not None

    def test_default_enabled_true(self) -> None:
        from code_indexer.server.utils.config_manager import QueryEmbeddingCacheConfig

        cfg = QueryEmbeddingCacheConfig()
        assert cfg.query_embedding_cache_enabled is True

    def test_default_voyage_mode_shadow(self) -> None:
        from code_indexer.server.utils.config_manager import QueryEmbeddingCacheConfig

        cfg = QueryEmbeddingCacheConfig()
        assert cfg.query_embedding_cache_voyage_mode == "shadow"

    def test_default_cohere_mode_shadow(self) -> None:
        from code_indexer.server.utils.config_manager import QueryEmbeddingCacheConfig

        cfg = QueryEmbeddingCacheConfig()
        assert cfg.query_embedding_cache_cohere_mode == "shadow"

    def test_default_max_entries(self) -> None:
        from code_indexer.server.utils.config_manager import QueryEmbeddingCacheConfig

        cfg = QueryEmbeddingCacheConfig()
        assert cfg.query_embedding_cache_max_entries == 10000

    def test_default_anchor_tokens(self) -> None:
        from code_indexer.server.utils.config_manager import QueryEmbeddingCacheConfig

        cfg = QueryEmbeddingCacheConfig()
        assert cfg.query_embedding_cache_anchor_tokens == 2

    def test_default_audit_sample_rate(self) -> None:
        from code_indexer.server.utils.config_manager import QueryEmbeddingCacheConfig

        cfg = QueryEmbeddingCacheConfig()
        assert cfg.query_embedding_cache_audit_sample_rate == 0.0

    def test_server_config_has_query_embedding_cache_config_field(self) -> None:
        import dataclasses

        from code_indexer.server.utils.config_manager import ServerConfig

        fields = {f.name for f in dataclasses.fields(ServerConfig)}
        assert "query_embedding_cache_config" in fields, (
            "ServerConfig must declare query_embedding_cache_config field"
        )

    def test_server_config_initializes_field_on_default_construction(
        self, tmp_path: Path
    ) -> None:
        from code_indexer.server.utils.config_manager import ServerConfig

        cfg = ServerConfig(server_dir=str(tmp_path))
        assert cfg.query_embedding_cache_config is not None, (
            "__post_init__ must initialize query_embedding_cache_config"
        )

    def test_server_config_field_is_qec_type(self, tmp_path: Path) -> None:
        from code_indexer.server.utils.config_manager import (
            QueryEmbeddingCacheConfig,
            ServerConfig,
        )

        cfg = ServerConfig(server_dir=str(tmp_path))
        assert isinstance(cfg.query_embedding_cache_config, QueryEmbeddingCacheConfig)


# ---------------------------------------------------------------------------
# TestQueryEmbeddingCacheLiveConfigReads
# ---------------------------------------------------------------------------


class TestQueryEmbeddingCacheLiveConfigReads:
    """AC3: enabled_for() and mode_for() read LIVE from the config service."""

    def _make_cache(self, tmp_path: Path):
        from code_indexer.server.services.query_embedding_cache import (
            QueryEmbeddingCache,
        )
        from code_indexer.server.storage.sqlite_backends import (
            QueryEmbeddingCacheSqliteBackend,
        )

        backend = QueryEmbeddingCacheSqliteBackend(str(tmp_path / "qec.db"))
        # Construction defaults: enabled=True, voyage_mode="shadow"
        return QueryEmbeddingCache(backend=backend)

    def test_kill_switch_live_read_disables_without_reconstruction(
        self, tmp_path: Path
    ) -> None:
        """Flipping query_embedding_cache_enabled=False in live config disables cache
        WITHOUT reconstructing the QueryEmbeddingCache instance."""
        cache = self._make_cache(tmp_path)

        # Initially enabled (live config returns enabled=True)
        live_cfg_on = _make_live_cfg(_make_qec_cfg(enabled=True))
        with patch(
            "code_indexer.server.services.query_embedding_cache.QueryEmbeddingCache._live_qec_cfg",
            return_value=live_cfg_on.query_embedding_cache_config,
        ):
            assert cache.enabled_for("voyage-ai") is True

        # Now flip to disabled in live config — same cache instance, no reconstruction
        live_cfg_off = _make_live_cfg(_make_qec_cfg(enabled=False))
        with patch(
            "code_indexer.server.services.query_embedding_cache.QueryEmbeddingCache._live_qec_cfg",
            return_value=live_cfg_off.query_embedding_cache_config,
        ):
            assert cache.enabled_for("voyage-ai") is False, (
                "Kill switch must take effect LIVE without reconstruction"
            )

    def test_voyage_mode_live_read_switches_from_shadow_to_on(
        self, tmp_path: Path
    ) -> None:
        """Changing voyage mode from shadow to on in live config reflects immediately."""
        cache = self._make_cache(tmp_path)

        shadow_cfg = _make_live_cfg(_make_qec_cfg(enabled=True, voyage_mode="shadow"))
        with patch(
            "code_indexer.server.services.query_embedding_cache.QueryEmbeddingCache._live_qec_cfg",
            return_value=shadow_cfg.query_embedding_cache_config,
        ):
            assert cache.mode_for("voyage-ai") == "shadow"

        on_cfg = _make_live_cfg(_make_qec_cfg(enabled=True, voyage_mode="on"))
        with patch(
            "code_indexer.server.services.query_embedding_cache.QueryEmbeddingCache._live_qec_cfg",
            return_value=on_cfg.query_embedding_cache_config,
        ):
            assert cache.mode_for("voyage-ai") == "on", (
                "Mode change must take effect LIVE without reconstruction"
            )

    def test_voyage_mode_live_read_off_disables_provider(self, tmp_path: Path) -> None:
        """Setting voyage mode to off disables cache for voyage-ai."""
        cache = self._make_cache(tmp_path)

        off_cfg = _make_live_cfg(_make_qec_cfg(enabled=True, voyage_mode="off"))
        with patch(
            "code_indexer.server.services.query_embedding_cache.QueryEmbeddingCache._live_qec_cfg",
            return_value=off_cfg.query_embedding_cache_config,
        ):
            assert cache.enabled_for("voyage-ai") is False
            assert cache.mode_for("voyage-ai") == "off"

    def test_cohere_mode_live_read(self, tmp_path: Path) -> None:
        """cohere_mode live read is independent of voyage_mode."""
        cache = self._make_cache(tmp_path)

        cfg = _make_live_cfg(
            _make_qec_cfg(enabled=True, voyage_mode="on", cohere_mode="off")
        )
        with patch(
            "code_indexer.server.services.query_embedding_cache.QueryEmbeddingCache._live_qec_cfg",
            return_value=cfg.query_embedding_cache_config,
        ):
            assert cache.mode_for("voyage-ai") == "on"
            assert cache.mode_for("cohere") == "off"
            assert cache.enabled_for("voyage-ai") is True
            assert cache.enabled_for("cohere") is False

    def test_fail_open_when_config_service_unavailable(self, tmp_path: Path) -> None:
        """When _live_qec_cfg raises, construction-time defaults are used (no exception)."""
        from code_indexer.server.services.query_embedding_cache import (
            QueryEmbeddingCache,
        )
        from code_indexer.server.storage.sqlite_backends import (
            QueryEmbeddingCacheSqliteBackend,
        )

        backend = QueryEmbeddingCacheSqliteBackend(str(tmp_path / "qec_fo.db"))
        # Construction default: enabled=True, voyage_mode="shadow"
        cache = QueryEmbeddingCache(backend=backend)

        with patch(
            "code_indexer.server.services.query_embedding_cache.QueryEmbeddingCache._live_qec_cfg",
            return_value=None,  # config service unavailable → fall back to construction defaults
        ):
            # Construction-time defaults apply: enabled=True, shadow
            assert cache.enabled_for("voyage-ai") is True
            assert cache.mode_for("voyage-ai") == "shadow"

    def test_construction_time_disabled_used_when_config_unavailable(
        self, tmp_path: Path
    ) -> None:
        """When config service unavailable, explicit constructor enabled=False is honored."""
        from code_indexer.server.services.query_embedding_cache import (
            QueryEmbeddingCache,
        )
        from code_indexer.server.storage.sqlite_backends import (
            QueryEmbeddingCacheSqliteBackend,
        )

        backend = QueryEmbeddingCacheSqliteBackend(str(tmp_path / "qec_dis.db"))
        cache = QueryEmbeddingCache(
            backend=backend, enabled=False, voyage_mode="on", cohere_mode="on"
        )

        with patch(
            "code_indexer.server.services.query_embedding_cache.QueryEmbeddingCache._live_qec_cfg",
            return_value=None,
        ):
            assert cache.enabled_for("voyage-ai") is False
