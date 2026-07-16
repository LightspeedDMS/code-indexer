"""
Unit tests for GoldenReposConfig.externally_managed (EVO-64493).

The flag lets an external owner manage golden-repo presence/refresh; the server
then only indexes/serves. These tests pin: defaults to False, round-trips
through save/load, coexists with the other golden-repo fields, and old configs
lacking the key still load (backward compatible).
"""

import json
from pathlib import Path

from code_indexer.server.utils.config_manager import (
    ServerConfigManager,
    GoldenReposConfig,
)


def test_externally_managed_defaults_to_false():
    """New GoldenReposConfig defaults externally_managed to False (self-managed)."""
    config = GoldenReposConfig()
    assert config.externally_managed is False


def test_externally_managed_saves_and_loads(tmp_path: Path):
    """externally_managed persists through a save/load cycle."""
    config_manager = ServerConfigManager(str(tmp_path))
    config = config_manager.create_default_config()

    assert config.golden_repos_config is not None
    config.golden_repos_config.externally_managed = True

    config_manager.save_config(config)
    loaded_config = config_manager.load_config()

    assert loaded_config is not None
    assert loaded_config.golden_repos_config is not None
    assert loaded_config.golden_repos_config.externally_managed is True


def test_externally_managed_missing_from_old_config(tmp_path: Path):
    """An old config without the key loads with externally_managed defaulting False."""
    config_data = {
        "server_dir": str(tmp_path),
        "golden_repos_config": {
            "refresh_interval_seconds": 3600,
            "analysis_model": "opus",
        },
    }
    config_file = tmp_path / "config.json"
    with open(config_file, "w") as f:
        json.dump(config_data, f)

    config_manager = ServerConfigManager(str(tmp_path))
    loaded_config = config_manager.load_config()

    assert loaded_config is not None
    assert loaded_config.golden_repos_config is not None
    assert loaded_config.golden_repos_config.externally_managed is False


def test_externally_managed_coexists_with_other_golden_fields(tmp_path: Path):
    """externally_managed does not disturb refresh_interval_seconds / analysis_model."""
    config_manager = ServerConfigManager(str(tmp_path))
    config = config_manager.create_default_config()

    assert config.golden_repos_config is not None
    config.golden_repos_config.refresh_interval_seconds = 120
    config.golden_repos_config.analysis_model = "sonnet"
    config.golden_repos_config.externally_managed = True

    config_manager.save_config(config)
    loaded_config = config_manager.load_config()

    assert loaded_config is not None
    assert loaded_config.golden_repos_config is not None
    gr = loaded_config.golden_repos_config
    assert gr.refresh_interval_seconds == 120
    assert gr.analysis_model == "sonnet"
    assert gr.externally_managed is True
