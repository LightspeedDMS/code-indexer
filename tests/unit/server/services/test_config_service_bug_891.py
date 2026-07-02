"""
Tests for Bug #891: memory_retrieval_config arrives as dict after _merge_runtime_config.

Root cause: _dict_to_server_config in config_manager.py handled many nested config
types but missed MemoryRetrievalConfig, causing search.py:847 to crash with
AttributeError: 'dict' object has no attribute 'memory_retrieval_enabled'.

Fix location: config_manager.py _dict_to_server_config — add conversion block for
memory_retrieval_config (same pattern as all other nested configs already there).

Scope of this file:
  - Tests 1-5: _dict_to_server_config coercion of memory_retrieval_config dict
  - Tests 6-8: exact crash-site regression (search.py:847, search.py:537, asdict round-trip)
  - Test 9: downstream regression for get_all_settings() after inline workaround removal
    at config_service.py:586-596 (explicitly included to guard that call site)
"""

import os
from dataclasses import asdict

import pytest

from code_indexer.server.utils.config_manager import (
    MemoryRetrievalConfig,
    ServerConfigManager,
)


@pytest.fixture
def config_manager(tmp_path):
    """ServerConfigManager pointed at a temp directory."""
    server_dir = str(tmp_path / "cidx-server")
    os.makedirs(server_dir, exist_ok=True)
    return ServerConfigManager(server_dir)


@pytest.fixture
def memory_retrieval_dict_config(tmp_path):
    """Minimal dict with memory_retrieval_config as a raw dict (pre-fix state)."""
    return {
        "server_dir": str(tmp_path / "cidx-server"),
        "memory_retrieval_config": {
            "memory_retrieval_enabled": True,
            "memory_voyage_min_score": 0.6,
            "memory_cohere_min_score": 0.5,
            "memory_retrieval_k_multiplier": 3,
            "memory_retrieval_max_body_chars": 1500,
        },
    }


class TestDictToServerConfigCoercesMemoryRetrievalConfig:
    """
    Bug #891: _dict_to_server_config must coerce memory_retrieval_config dict to dataclass.

    When ConfigService._merge_runtime_config runs:
      1. asdict(config) converts MemoryRetrievalConfig to a plain dict
      2. runtime overrides are merged in
      3. _dict_to_server_config is called to reconstruct ServerConfig

    Step 3 must re-coerce the dict back to MemoryRetrievalConfig.  Prior to the fix,
    step 3 had no conversion block for memory_retrieval_config, leaving it as a dict.
    """

    def test_merge_runtime_config_coerces_dict_nested_configs_to_dataclass(
        self, config_manager, memory_retrieval_dict_config
    ):
        """After _dict_to_server_config, memory_retrieval_config must be a dataclass instance."""
        config = config_manager._dict_to_server_config(memory_retrieval_dict_config)

        assert isinstance(config.memory_retrieval_config, MemoryRetrievalConfig), (
            f"Expected MemoryRetrievalConfig, got {type(config.memory_retrieval_config)}"
        )

    def test_memory_retrieval_config_dict_preserves_all_field_values(
        self, config_manager, memory_retrieval_dict_config
    ):
        """All five MemoryRetrievalConfig fields must survive dict-to-dataclass coercion."""
        config = config_manager._dict_to_server_config(memory_retrieval_dict_config)
        mem = config.memory_retrieval_config

        assert mem.memory_retrieval_enabled is True
        assert mem.memory_voyage_min_score == 0.6
        assert mem.memory_cohere_min_score == 0.5
        assert mem.memory_retrieval_k_multiplier == 3
        assert mem.memory_retrieval_max_body_chars == 1500

    def test_merge_runtime_config_preserves_dataclass_when_already_correct(
        self, config_manager, tmp_path
    ):
        """If input already has a MemoryRetrievalConfig instance, it must not be re-wrapped."""
        cfg_dict = {
            "server_dir": str(tmp_path / "cidx-server"),
            "memory_retrieval_config": MemoryRetrievalConfig(
                memory_retrieval_enabled=False,
                memory_voyage_min_score=0.7,
            ),
        }

        config = config_manager._dict_to_server_config(cfg_dict)

        assert isinstance(config.memory_retrieval_config, MemoryRetrievalConfig)
        assert config.memory_retrieval_config.memory_retrieval_enabled is False
        assert config.memory_retrieval_config.memory_voyage_min_score == 0.7

    def test_merge_runtime_config_filters_unknown_keys_in_memory_retrieval_config(
        self, config_manager, tmp_path
    ):
        """Unknown keys in the dict must be silently dropped without raising TypeError."""
        cfg_dict = {
            "server_dir": str(tmp_path / "cidx-server"),
            "memory_retrieval_config": {
                "memory_retrieval_enabled": True,
                "memory_voyage_min_score": 0.55,
                "unknown_future_field": "should_be_ignored",
                "another_unknown": 42,
            },
        }

        # Must not raise TypeError due to unexpected kwargs
        config = config_manager._dict_to_server_config(cfg_dict)

        assert isinstance(config.memory_retrieval_config, MemoryRetrievalConfig)
        assert config.memory_retrieval_config.memory_retrieval_enabled is True
        assert config.memory_retrieval_config.memory_voyage_min_score == 0.55

    def test_merge_runtime_config_none_memory_retrieval_is_normalized_to_dataclass(
        self, config_manager, tmp_path
    ):
        """None for memory_retrieval_config is normalized to MemoryRetrievalConfig() by __post_init__."""
        cfg_dict = {
            "server_dir": str(tmp_path / "cidx-server"),
            "memory_retrieval_config": None,
        }

        config = config_manager._dict_to_server_config(cfg_dict)

        # ServerConfig.__post_init__ sets it to MemoryRetrievalConfig() when None.
        assert isinstance(config.memory_retrieval_config, MemoryRetrievalConfig)


class TestSearchCodeNoLongerCrashesOnDictMemoryConfig:
    """
    Regression: confirm the 3 failing MCP tests no longer hit
    AttributeError: 'dict' object has no attribute 'memory_retrieval_enabled'.

    These tests replicate the exact trigger condition — a ServerConfig whose
    memory_retrieval_config is a plain dict — and exercise the attribute access
    pattern that was crashing (search.py:847 and search.py:537).
    """

    def test_attribute_access_works_after_dict_coercion(
        self, config_manager, memory_retrieval_dict_config
    ):
        """
        Simulates the crash site search.py:847:
            if mem_cfg.memory_retrieval_enabled:  # AttributeError before fix
        """
        config = config_manager._dict_to_server_config(memory_retrieval_dict_config)
        mem_cfg = config.memory_retrieval_config

        # Exact access pattern from search.py:847 — must not raise AttributeError.
        enabled = mem_cfg.memory_retrieval_enabled
        assert enabled is True

    def test_attribute_access_works_on_second_crash_site(
        self, config_manager, memory_retrieval_dict_config
    ):
        """
        Simulates search.py:537 (_compute_shared_query_vector):
            if not raw_mem_cfg.memory_retrieval_enabled:  # AttributeError before fix
        """
        config = config_manager._dict_to_server_config(memory_retrieval_dict_config)
        raw_mem_cfg = config.memory_retrieval_config

        # Exact access pattern from search.py:537 — must not raise AttributeError.
        enabled = raw_mem_cfg.memory_retrieval_enabled
        assert isinstance(enabled, bool)

    def test_dict_to_server_config_coercion_covers_asdict_round_trip(
        self, config_manager, tmp_path
    ):
        """
        Reproduces the real failure path: asdict() round-trip through _dict_to_server_config.

        _merge_runtime_config does:
          full_dict = asdict(config)          <- MemoryRetrievalConfig becomes dict
          new_config = _dict_to_server_config(full_dict)  <- must reconvert to dataclass

        This test exercises that exact round-trip and asserts the result is a dataclass.
        """
        original = config_manager._dict_to_server_config(
            {
                "server_dir": str(tmp_path / "cidx-server"),
                "memory_retrieval_config": MemoryRetrievalConfig(
                    memory_retrieval_enabled=True,
                    memory_voyage_min_score=0.65,
                    memory_retrieval_k_multiplier=7,
                ),
            }
        )
        assert isinstance(original.memory_retrieval_config, MemoryRetrievalConfig)

        # Simulate _merge_runtime_config: asdict -> dict -> _dict_to_server_config
        full_dict = asdict(original)
        assert isinstance(full_dict["memory_retrieval_config"], dict), (
            "asdict must have converted MemoryRetrievalConfig to a plain dict"
        )

        reconstructed = config_manager._dict_to_server_config(full_dict)

        assert isinstance(
            reconstructed.memory_retrieval_config, MemoryRetrievalConfig
        ), (
            f"Round-trip must yield MemoryRetrievalConfig, "
            f"got {type(reconstructed.memory_retrieval_config)}"
        )
        assert reconstructed.memory_retrieval_config.memory_voyage_min_score == 0.65
        assert reconstructed.memory_retrieval_config.memory_retrieval_k_multiplier == 7


class TestGetAllSettingsAfterInlineWorkaroundRemoval:
    """
    Downstream regression: get_all_settings() must still work after the inline
    isinstance dict-coercion workaround at config_service.py:586-596 is removed.

    The workaround was a symptom; the fix is in _dict_to_server_config so that
    memory_retrieval_config is always a dataclass by the time get_all_settings() runs.
    This test is explicitly included to guard the get_all_settings() call site.
    """

    def test_get_all_settings_returns_memory_retrieval_settings(self, tmp_path):
        """get_all_settings() must return all memory_retrieval fields without error."""
        from code_indexer.server.services.config_service import ConfigService

        server_dir = str(tmp_path / "cidx-server")
        os.makedirs(server_dir, exist_ok=True)

        svc = ConfigService(server_dir)
        config = svc.load_config()

        # Confirm asdict produces a dict (pre-fix state that triggered the original bug)
        full_dict = asdict(config)
        assert isinstance(full_dict["memory_retrieval_config"], dict), (
            "asdict must produce a dict for memory_retrieval_config"
        )

        # After the fix, _dict_to_server_config normalizes dict to dataclass
        mgr = ServerConfigManager(server_dir)
        normalized = mgr._dict_to_server_config(full_dict)
        assert isinstance(normalized.memory_retrieval_config, MemoryRetrievalConfig)

        svc._config = normalized

        settings = svc.get_all_settings()
        assert "memory_retrieval" in settings
        mr = settings["memory_retrieval"]
        assert "memory_retrieval_enabled" in mr
        assert isinstance(mr["memory_retrieval_enabled"], bool)
        assert "memory_voyage_min_score" in mr
        assert "memory_cohere_min_score" in mr
        assert "memory_retrieval_k_multiplier" in mr
        assert "memory_retrieval_max_body_chars" in mr
