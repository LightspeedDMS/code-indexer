"""
Unit tests for Bug #1226: launch_restart_generation must be in EXPECTED_ORPHAN_KEYS.

Story #1195 / #1198 wrote `launch_restart_generation` (a RUNTIME DB config key) into
the bootstrap config.json.  The bootstrap `ServerConfig` dataclass correctly does NOT
have this field, so the loader strips it -- but before this fix it did so at WARNING
level, flooding cluster restart logs (~22 entries per restart).

Fix: add the key to EXPECTED_ORPHAN_KEYS so it is stripped silently (INFO only).
"""

import json
import logging

from code_indexer.server.utils.config_manager import (
    EXPECTED_ORPHAN_KEYS,
    ServerConfigManager,
)


class TestLaunchRestartGenerationOrphanKey:
    """Bug #1226 -- launch_restart_generation must not flood WARNING on config load."""

    def test_launch_restart_generation_in_expected_orphan_keys(self):
        """launch_restart_generation must be a member of EXPECTED_ORPHAN_KEYS."""
        assert "launch_restart_generation" in EXPECTED_ORPHAN_KEYS, (
            "launch_restart_generation must be in EXPECTED_ORPHAN_KEYS "
            "(Story #1195/#1198 runtime key written into config.json by auto-updater)"
        )

    def test_launch_restart_generation_no_warning(self, tmp_path, caplog):
        """Loading a config.json containing launch_restart_generation must NOT emit a WARNING.

        The key is stripped silently (INFO only) as an expected-orphan, not as an
        unknown key -- the WARNING path is reserved for genuinely unknown/typo keys.
        """
        config_manager = ServerConfigManager(str(tmp_path))
        config = config_manager.create_default_config()
        config_manager.save_config(config)

        config_path = tmp_path / "config.json"
        raw = json.loads(config_path.read_text())
        # Inject the key exactly as the auto-updater / Story #1195 writes it
        raw["launch_restart_generation"] = True
        config_path.write_text(json.dumps(raw))

        with caplog.at_level(
            logging.WARNING, logger="code_indexer.server.utils.config_manager"
        ):
            loaded = config_manager.load_config()

        assert loaded is not None, "load_config() must return a valid ServerConfig"

        warning_messages = [
            r.message for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert not any(
            "launch_restart_generation" in msg for msg in warning_messages
        ), (
            "Expected NO WARNING for launch_restart_generation (it must be in "
            f"EXPECTED_ORPHAN_KEYS), but got: {warning_messages}"
        )

    def test_bogus_key_still_warns(self, tmp_path, caplog):
        """Regression: a key NOT in EXPECTED_ORPHAN_KEYS must still log a WARNING.

        The WARNING path for true unknowns / typos must be preserved -- this fix must
        not accidentally suppress all unknown-key warnings.
        """
        config_manager = ServerConfigManager(str(tmp_path))
        config = config_manager.create_default_config()
        config_manager.save_config(config)

        config_path = tmp_path / "config.json"
        raw = json.loads(config_path.read_text())
        raw["totally_bogus_key_xyz"] = {"value": 42}
        config_path.write_text(json.dumps(raw))

        with caplog.at_level(
            logging.WARNING, logger="code_indexer.server.utils.config_manager"
        ):
            loaded = config_manager.load_config()

        assert loaded is not None
        warning_messages = [
            r.message for r in caplog.records if r.levelno == logging.WARNING
        ]
        assert any("totally_bogus_key_xyz" in msg for msg in warning_messages), (
            f"Expected WARNING for truly unknown key 'totally_bogus_key_xyz', got: {warning_messages}"
        )


class TestQueryVectorCacheConfigOrphanKey:
    """EVO-70528 -- the fork-only query_vector_cache_config key is gone from
    ServerConfig but is still in persisted config.json files; it must not log a
    WARNING on every process start (the runtime QueryEmbeddingCacheConfig
    replaced it, with a different shape, so there is nothing to migrate)."""

    def test_query_vector_cache_config_no_warning(self, tmp_path, caplog):
        config_manager = ServerConfigManager(str(tmp_path))
        config_manager.save_config(config_manager.create_default_config())
        config_path = tmp_path / "config.json"
        raw = json.loads(config_path.read_text())
        raw["query_vector_cache_config"] = {"enabled": False, "max_entries": 10000}
        config_path.write_text(json.dumps(raw))

        with caplog.at_level(
            logging.WARNING, logger="code_indexer.server.utils.config_manager"
        ):
            loaded = config_manager.load_config()

        assert loaded is not None
        assert not [
            r.message
            for r in caplog.records
            if r.levelno == logging.WARNING and "query_vector_cache_config" in r.message
        ]
        assert not hasattr(loaded, "query_vector_cache_config")
