"""Bug #1376: TemporalConfig.active_embedder=None (or otherwise invalid)
must NOT take down the entire Config model.

Root cause: TemporalConfig.active_embedder was a REQUIRED str, so a
config.json with "temporal": {"active_embedder": null, ...} (administratively
disabled temporal) blew up Config(**data) validation for the WHOLE repo's
config -- including completely unrelated non-temporal queries.

Fix:
1. TemporalConfig.active_embedder becomes Optional[str] (None = disabled).
2. The membership model_validator only enforces membership when active_embedder
   is not None; a non-None value not in embedders still raises (direct
   construction contract, locked in by the #1291 regression test).
3. Config.temporal gets a mode="before" field_validator that tolerates a
   malformed dict by degrading to a valid, disabled TemporalConfig
   (active_embedder=None), logging a de-duplicated WARNING instead of
   propagating the exception.
"""

import json
import logging

import pytest

from code_indexer.config import (
    Config,
    ConfigManager,
    TemporalConfig,
    _reset_temporal_config_warn_memo_for_tests,
)


@pytest.fixture(autouse=True)
def _reset_temporal_warn_memo():
    """Ensure the de-dup memo doesn't leak warning-suppression across tests."""
    _reset_temporal_config_warn_memo_for_tests()
    yield
    _reset_temporal_config_warn_memo_for_tests()


class TestConfigLevelNullActiveEmbedderTolerance:
    def test_config_construct_with_null_active_embedder_does_not_raise(self):
        """Config(**data) with temporal.active_embedder=None must construct
        cleanly -- this is the core blast-radius fix."""
        result = Config(
            **{
                "temporal": {
                    "active_embedder": None,
                    "embedders": ["voyage-context-4"],
                }
            }
        )
        assert result.temporal.active_embedder is None

    def test_config_manager_load_round_trip_with_null_active_embedder(self, tmp_path):
        """A real config.json on disk with a null active_embedder must load
        successfully via ConfigManager.load(), and unrelated fields (e.g.
        codebase_dir) must be completely unaffected."""
        code_indexer_dir = tmp_path / ".code-indexer"
        code_indexer_dir.mkdir()
        config_path = code_indexer_dir / "config.json"
        config_data = {
            "codebase_dir": str(tmp_path),
            "temporal": {
                "active_embedder": None,
                "embedders": ["voyage-context-4"],
            },
        }
        config_path.write_text(json.dumps(config_data))

        manager = ConfigManager(config_path)
        config = manager.load()

        assert config.temporal.active_embedder is None
        # Unrelated, non-temporal state must load correctly -- proving the
        # blast radius is contained to the temporal subsystem only.
        assert config.codebase_dir == tmp_path.resolve()
        assert "py" in config.file_extensions

    def test_config_default_temporal_active_embedder_unaffected(self):
        """Regression guard: Config() with no temporal key (or explicit
        default) must still default active_embedder to voyage-context-4."""
        result = Config()
        assert result.temporal.active_embedder == "voyage-context-4"

        result_explicit_default = Config(**{"temporal": TemporalConfig()})
        assert result_explicit_default.temporal.active_embedder == ("voyage-context-4")

    def test_config_construct_with_non_member_active_embedder_degrades(self):
        """A non-null but INVALID active_embedder (not a member of embedders)
        embedded inside a full Config dict must also degrade gracefully --
        not just literal null."""
        result = Config(
            **{
                "temporal": {
                    "embedders": ["voyage-context-4"],
                    "active_embedder": "embed-v4.0",
                }
            }
        )
        assert result.temporal.active_embedder is None


class TestTemporalConfigDirectConstructionAllowsNone:
    def test_direct_temporal_config_construction_with_none_does_not_raise(self):
        """None is a legitimate first-class value on TemporalConfig itself,
        not just something Config's before-validator invents."""
        config = TemporalConfig(embedders=["voyage-context-4"], active_embedder=None)
        assert config.active_embedder is None


class TestConfigLevelDegradationWarningDedup:
    def test_warning_logged_once_for_same_invalid_temporal_dict(self, caplog):
        invalid_temporal = {
            "embedders": ["voyage-context-4"],
            "active_embedder": "embed-v4.0",
        }
        with caplog.at_level(logging.WARNING, logger="code_indexer.config"):
            Config(**{"temporal": dict(invalid_temporal)})
            Config(**{"temporal": dict(invalid_temporal)})

        warning_records = [
            r
            for r in caplog.records
            if r.levelno == logging.WARNING and "temporal" in r.getMessage().lower()
        ]
        assert len(warning_records) == 1, (
            "Expected exactly one de-duplicated WARNING for repeated "
            f"identical invalid temporal config, got {len(warning_records)}: "
            f"{[r.getMessage() for r in warning_records]}"
        )
