"""
Bug #891 after modular phase 4f: stored memory retrieval settings survive.

Bug #891 was a stored ``memory_retrieval_config`` dict reaching the search
handler unconverted. The settings are now the cidx-memory feature's config
section (``feature_settings["memory"]``): ``_dict_to_server_config`` moves a
stored ``memory_retrieval_config`` (a ``config.json`` value or the runtime
database row, written before the move) into the section, and the feature
reads its section through the ``config`` capability. These tests pin that
move -- values preserved, an explicit section value winning, nothing left
behind for the core to trip over -- and the feature config door's listing.
"""

import os
from dataclasses import asdict

import pytest

from code_indexer.server.utils.config_manager import ServerConfigManager

_STORED = {
    "memory_retrieval_enabled": True,
    "memory_voyage_min_score": 0.6,
    "memory_cohere_min_score": 0.5,
    "memory_retrieval_k_multiplier": 3,
    "memory_retrieval_max_body_chars": 1500,
}


@pytest.fixture
def config_manager(tmp_path):
    """ServerConfigManager pointed at a temp directory."""
    server_dir = str(tmp_path / "cidx-server")
    os.makedirs(server_dir, exist_ok=True)
    return ServerConfigManager(server_dir)


@pytest.fixture
def memory_retrieval_dict_config(tmp_path):
    """A stored configuration from before the move (the Bug #891 shape)."""
    return {
        "server_dir": str(tmp_path / "cidx-server"),
        "memory_retrieval_config": dict(_STORED),
    }


class TestStoredMemoryRetrievalConfigMovesIntoTheFeatureSection:
    def test_the_dict_becomes_the_memory_section(
        self, config_manager, memory_retrieval_dict_config
    ):
        config = config_manager._dict_to_server_config(memory_retrieval_dict_config)

        assert config.feature_settings["memory"] == _STORED
        assert not hasattr(config, "memory_retrieval_config")

    def test_an_explicit_section_value_wins(self, config_manager, tmp_path):
        config = config_manager._dict_to_server_config(
            {
                "server_dir": str(tmp_path / "cidx-server"),
                "memory_retrieval_config": {"memory_voyage_min_score": 0.55},
                "feature_settings": {"memory": {"memory_voyage_min_score": 0.7}},
            }
        )

        assert config.feature_settings["memory"]["memory_voyage_min_score"] == 0.7

    def test_none_moves_nothing(self, config_manager, tmp_path):
        config = config_manager._dict_to_server_config(
            {
                "server_dir": str(tmp_path / "cidx-server"),
                "memory_retrieval_config": None,
            }
        )

        assert "memory" not in (config.feature_settings or {})

    def test_the_asdict_round_trip_keeps_the_section(
        self, config_manager, memory_retrieval_dict_config
    ):
        """_merge_runtime_config does asdict -> merge -> _dict_to_server_config:
        the section must come back as it went in."""
        original = config_manager._dict_to_server_config(memory_retrieval_dict_config)

        reconstructed = config_manager._dict_to_server_config(asdict(original))

        assert reconstructed.feature_settings["memory"] == _STORED


class TestTheMovedSectionIsListedByTheFeatureDoor:
    def test_listed_with_the_stored_values(self, config_manager, tmp_path):
        """The config API / screen lists the moved values (the generic
        Feature Settings block; the core lists no memory category)."""
        from cidx_memory.config import MEMORY_CONFIG_SECTION

        from code_indexer.server.features.config import describe_sections

        config = config_manager._dict_to_server_config(
            {
                "server_dir": str(tmp_path / "cidx-server"),
                "memory_retrieval_config": dict(_STORED),
            }
        )

        (section,) = describe_sections([("memory", MEMORY_CONFIG_SECTION)], config)

        assert {key["name"]: key["value"] for key in section["keys"]} == _STORED

    def test_the_core_lists_no_memory_category(self, tmp_path):
        from code_indexer.server.services.config_service import ConfigService

        server_dir = str(tmp_path / "cidx-server")
        os.makedirs(server_dir, exist_ok=True)
        svc = ConfigService(server_dir)
        svc.load_config()

        assert "memory_retrieval" not in svc.get_all_settings()
