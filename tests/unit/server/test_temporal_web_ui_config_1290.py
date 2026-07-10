"""Unit tests for Story #1290 - Web UI exposure of temporal per-commit config.

Covers the 3 TemporalConfig fields (temporal.embedders, temporal.active_embedder,
temporal.aggregation_chunk_chars) surfaced on the Web UI Config Screen via
get_config_service(), mirroring the Story #1158 parallel-requests pattern:
- IndexingConfig default values for the 3 new fields
- _update_indexing_setting() handling of the new keys with validation
- _get_server_provider_values() (config_seeding) propagation into config.json
"""

import pytest

from code_indexer.server.services.config_service import ConfigService
from code_indexer.server.utils.config_manager import IndexingConfig, ServerConfigManager


@pytest.fixture
def svc(tmp_path) -> ConfigService:
    """Create a ConfigService instance backed by real SQLite in tmp_path."""
    mgr = ServerConfigManager(server_dir_path=str(tmp_path))
    return ConfigService(config_manager=mgr)


# ---------------------------------------------------------------------------
# IndexingConfig defaults
# ---------------------------------------------------------------------------


class TestTemporalIndexingConfigDefaults:
    def test_temporal_embedders_default(self) -> None:
        cfg = IndexingConfig()
        assert cfg.temporal_embedders == ["voyage-context-4"]

    def test_temporal_active_embedder_default(self) -> None:
        cfg = IndexingConfig()
        assert cfg.temporal_active_embedder == "voyage-context-4"

    def test_temporal_aggregation_chunk_chars_default(self) -> None:
        cfg = IndexingConfig()
        assert cfg.temporal_aggregation_chunk_chars == 4096


# ---------------------------------------------------------------------------
# _update_indexing_setting() new keys
# ---------------------------------------------------------------------------


class TestUpdateTemporalIndexingSetting:
    def test_temporal_embedders_accepts_comma_separated_string(self, svc) -> None:
        svc._update_indexing_setting("temporal_embedders", "voyage-context-4")
        cfg = svc.get_config()
        assert cfg.indexing_config.temporal_embedders == ["voyage-context-4"]

    def test_temporal_embedders_rejects_empty(self, svc) -> None:
        with pytest.raises(ValueError):
            svc._update_indexing_setting("temporal_embedders", "")

    def test_temporal_active_embedder_must_be_member_of_embedders(self, svc) -> None:
        svc._update_indexing_setting("temporal_embedders", "voyage-context-4")
        with pytest.raises(ValueError):
            svc._update_indexing_setting("temporal_active_embedder", "bogus-model")

    def test_temporal_active_embedder_accepted_when_member(self, svc) -> None:
        svc._update_indexing_setting("temporal_embedders", "voyage-context-4")
        svc._update_indexing_setting("temporal_active_embedder", "voyage-context-4")
        cfg = svc.get_config()
        assert cfg.indexing_config.temporal_active_embedder == "voyage-context-4"

    def test_temporal_aggregation_chunk_chars_stored(self, svc) -> None:
        svc._update_indexing_setting("temporal_aggregation_chunk_chars", "8192")
        cfg = svc.get_config()
        assert cfg.indexing_config.temporal_aggregation_chunk_chars == 8192

    def test_temporal_aggregation_chunk_chars_rejects_non_positive(self, svc) -> None:
        with pytest.raises(ValueError):
            svc._update_indexing_setting("temporal_aggregation_chunk_chars", "0")


# ---------------------------------------------------------------------------
# get_all_settings() display wiring
# ---------------------------------------------------------------------------


class TestGetAllSettingsTemporalFields:
    def test_indexing_dict_exposes_temporal_fields(self, svc) -> None:
        settings = svc.get_all_settings()
        indexing = settings["indexing"]
        assert "temporal_embedders" in indexing
        assert "temporal_active_embedder" in indexing
        assert "temporal_aggregation_chunk_chars" in indexing
        assert indexing["temporal_embedders"] == ["voyage-context-4"]
        assert indexing["temporal_active_embedder"] == "voyage-context-4"
        assert indexing["temporal_aggregation_chunk_chars"] == 4096


# ---------------------------------------------------------------------------
# config_seeding.py propagation into per-repo config.json
# ---------------------------------------------------------------------------


class TestValidateConfigSectionIndexingTemporal:
    """Route-level pre-submission validation for the 3 new indexing fields."""

    def _validate(self, data):
        from code_indexer.server.web.routes import _validate_config_section

        return _validate_config_section("indexing", data)

    def test_empty_temporal_embedders_returns_error(self) -> None:
        result = self._validate({"temporal_embedders": ""})
        assert result is not None
        assert "empty" in result.lower()

    def test_valid_temporal_embedders_returns_none(self) -> None:
        result = self._validate({"temporal_embedders": "voyage-context-4"})
        assert result is None

    def test_empty_temporal_active_embedder_returns_error(self) -> None:
        result = self._validate({"temporal_active_embedder": ""})
        assert result is not None
        assert "empty" in result.lower()

    def test_active_embedder_not_in_submitted_embedders_returns_error(self) -> None:
        result = self._validate(
            {
                "temporal_embedders": "voyage-context-4",
                "temporal_active_embedder": "bogus-model",
            }
        )
        assert result is not None
        assert "must be one of" in result.lower()

    def test_active_embedder_in_submitted_embedders_returns_none(self) -> None:
        result = self._validate(
            {
                "temporal_embedders": "voyage-context-4",
                "temporal_active_embedder": "voyage-context-4",
            }
        )
        assert result is None

    def test_non_positive_chunk_chars_returns_error(self) -> None:
        result = self._validate({"temporal_aggregation_chunk_chars": "0"})
        assert result is not None
        assert "positive" in result.lower()

    def test_non_numeric_chunk_chars_returns_error(self) -> None:
        result = self._validate({"temporal_aggregation_chunk_chars": "abc"})
        assert result is not None
        assert "valid" in result.lower()

    def test_valid_chunk_chars_returns_none(self) -> None:
        result = self._validate({"temporal_aggregation_chunk_chars": "4096"})
        assert result is None


class TestConfigSeedingTemporalPropagation:
    def test_seed_provider_config_propagates_temporal_fields(
        self, svc, tmp_path
    ) -> None:
        import json
        from unittest.mock import patch
        from code_indexer.server.services.config_seeding import seed_provider_config

        repo_path = tmp_path / "repo"
        cc_dir = repo_path / ".code-indexer"
        cc_dir.mkdir(parents=True)
        config_file = cc_dir / "config.json"
        config_file.write_text(json.dumps({"voyage_ai": {}, "cohere": {}}))

        svc._update_indexing_setting("temporal_embedders", "voyage-context-4")
        svc._update_indexing_setting("temporal_active_embedder", "voyage-context-4")
        svc._update_indexing_setting("temporal_aggregation_chunk_chars", "2048")

        with patch(
            "code_indexer.server.services.config_service.get_config_service",
            return_value=svc,
        ):
            seed_provider_config(str(repo_path))

        with open(config_file) as f:
            disk_config = json.load(f)

        assert disk_config["temporal"]["embedders"] == ["voyage-context-4"]
        assert disk_config["temporal"]["active_embedder"] == "voyage-context-4"
        assert disk_config["temporal"]["aggregation_chunk_chars"] == 2048
