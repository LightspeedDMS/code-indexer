"""
Regression tests for Bug #1006: enforce_pace_maker_pacing_only migration warning.

The old field 'enforce_pace_maker_pacing_only' (bool) was replaced by
'pace_maker_mode' (str: "on" / "disabled" / "off") in Story #997.

The migration logic was only in ServerConfig.__post_init__, but
_dict_to_server_config() strips unknown keys BEFORE constructing ServerConfig,
so __post_init__ never saw the old field -- resulting in 53+ repeated WARNINGs
per server restart.

Fix: migrate the field inside _dict_to_server_config() BEFORE stripping.
"""

import json
import logging
import tempfile

from src.code_indexer.server.utils.config_manager import ServerConfigManager


def _write_config(tmpdir: str, extra_fields: dict) -> ServerConfigManager:
    """Write a minimal config file with extra_fields merged; return the manager."""
    manager = ServerConfigManager(tmpdir)
    config_dict = {"server_dir": tmpdir, **extra_fields}
    with open(manager.config_file_path, "w") as f:
        json.dump(config_dict, f)
    return manager


def _load_config_from_dict(tmpdir: str, extra_fields: dict):
    """Write config and immediately load it; return the ServerConfig."""
    return _write_config(tmpdir, extra_fields).load_config()


class TestBug1006PaceMakerMigration:
    """Bug #1006: enforce_pace_maker_pacing_only must be migrated without warning."""

    def test_enforce_pace_maker_pacing_only_true_migrates_to_on(self):
        """
        Given a config with enforce_pace_maker_pacing_only=True
        When loaded via ServerConfigManager
        Then pace_maker_mode is 'on'
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            config = _load_config_from_dict(
                tmpdir, {"enforce_pace_maker_pacing_only": True}
            )
            assert config.pace_maker_mode == "on"

    def test_enforce_pace_maker_pacing_only_false_migrates_to_disabled(self):
        """
        Given a config with enforce_pace_maker_pacing_only=False
        When loaded via ServerConfigManager
        Then pace_maker_mode is 'disabled'
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            config = _load_config_from_dict(
                tmpdir, {"enforce_pace_maker_pacing_only": False}
            )
            assert config.pace_maker_mode == "disabled"

    def test_no_warning_logged_for_enforce_pace_maker_pacing_only(self, caplog):
        """
        Given a config with enforce_pace_maker_pacing_only present
        When loaded via ServerConfigManager
        Then no 'Stripped unknown config key' WARNING is emitted for that field
        """
        with tempfile.TemporaryDirectory() as tmpdir:
            manager = _write_config(tmpdir, {"enforce_pace_maker_pacing_only": True})
            with caplog.at_level(logging.WARNING):
                manager.load_config()

            offending = [
                r.message
                for r in caplog.records
                if r.levelno >= logging.WARNING
                and "enforce_pace_maker_pacing_only" in r.message
            ]
            assert offending == [], (
                f"Expected no warning for 'enforce_pace_maker_pacing_only', "
                f"but got: {offending}"
            )
