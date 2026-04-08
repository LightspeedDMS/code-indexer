"""
Unit tests for RerankConfig integration with ConfigService (Story #652).

Tests the RerankConfig dataclass, ServerConfig integration, and ConfigService
dispatch for the "rerank" category.

TDD: These tests are written BEFORE implementation to define expected behavior.
"""

import dataclasses
import shutil
import tempfile

import pytest

from code_indexer.server.services.config_service import ConfigService
from code_indexer.server.utils.config_manager import RerankConfig, ServerConfig


@pytest.fixture
def temp_dir():
    """Provide a temporary directory, cleaned up after each test."""
    directory = tempfile.mkdtemp()
    yield directory
    shutil.rmtree(directory)


@pytest.fixture
def config_service(temp_dir):
    """Provide a ConfigService backed by a fresh temp directory."""
    service = ConfigService(server_dir_path=temp_dir)
    service.load_config()
    return service


class TestRerankConfigDataclass:
    """AC1: RerankConfig dataclass exists with correct fields and defaults."""

    def test_rerank_config_can_be_imported(self):
        """RerankConfig should be importable from config_manager."""
        assert RerankConfig is not None

    def test_rerank_config_default_voyage_model_empty(self):
        """AC1: voyage_reranker_model defaults to empty string (disabled)."""
        assert RerankConfig().voyage_reranker_model == ""

    def test_rerank_config_default_cohere_model_empty(self):
        """AC1: cohere_reranker_model defaults to empty string (disabled)."""
        assert RerankConfig().cohere_reranker_model == ""

    def test_rerank_config_default_overfetch_multiplier(self):
        """AC1: overfetch_multiplier defaults to 5."""
        assert RerankConfig().overfetch_multiplier == 5

    def test_rerank_config_voyage_model_settable(self):
        """AC1: voyage_reranker_model can be set to a non-empty value."""
        assert (
            RerankConfig(voyage_reranker_model="rerank-2").voyage_reranker_model
            == "rerank-2"
        )

    def test_rerank_config_cohere_model_settable(self):
        """AC1: cohere_reranker_model can be set to a non-empty value."""
        assert (
            RerankConfig(
                cohere_reranker_model="rerank-english-v3.0"
            ).cohere_reranker_model
            == "rerank-english-v3.0"
        )

    def test_rerank_config_overfetch_multiplier_settable(self):
        """AC1: overfetch_multiplier can be set to custom value."""
        assert RerankConfig(overfetch_multiplier=10).overfetch_multiplier == 10

    def test_rerank_config_is_dataclass(self):
        """AC1: RerankConfig is a dataclass."""
        assert dataclasses.is_dataclass(RerankConfig)

    def test_rerank_config_serializes_to_dict(self):
        """AC1: RerankConfig serializes correctly via asdict."""
        config = RerankConfig(
            voyage_reranker_model="rerank-2",
            cohere_reranker_model="rerank-english-v3.0",
            overfetch_multiplier=7,
        )
        assert dataclasses.asdict(config) == {
            "voyage_reranker_model": "rerank-2",
            "cohere_reranker_model": "rerank-english-v3.0",
            "overfetch_multiplier": 7,
        }

    def test_rerank_config_deserializes_from_dict(self):
        """AC1: RerankConfig can be created from a dict of keyword args."""
        config = RerankConfig(
            voyage_reranker_model="rerank-2",
            cohere_reranker_model="",
            overfetch_multiplier=8,
        )
        assert config.voyage_reranker_model == "rerank-2"
        assert config.cohere_reranker_model == ""
        assert config.overfetch_multiplier == 8

    def test_rerank_config_has_no_overfetch_balanced_field(self):
        """AC1: overfetch_balanced field must NOT exist (replaced by overfetch_multiplier)."""
        assert not hasattr(RerankConfig(), "overfetch_balanced")

    def test_rerank_config_has_no_overfetch_high_field(self):
        """AC1: overfetch_high field must NOT exist (replaced by overfetch_multiplier)."""
        assert not hasattr(RerankConfig(), "overfetch_high")


class TestServerConfigRerankIntegration:
    """AC2 + AC5: ServerConfig has Optional rerank_config field, None by default."""

    def test_server_config_has_rerank_config_field(self, temp_dir):
        """AC2: ServerConfig should have rerank_config field."""
        assert hasattr(ServerConfig(server_dir=temp_dir), "rerank_config")

    def test_server_config_rerank_config_optional_accepts_instance(self, temp_dir):
        """AC2: rerank_config field accepts RerankConfig instances."""
        config = ServerConfig(server_dir=temp_dir)
        config.rerank_config = RerankConfig(voyage_reranker_model="rerank-2")
        assert config.rerank_config.voyage_reranker_model == "rerank-2"

    def test_consumers_use_defaults_when_none(self, temp_dir):
        """AC2/AC5: None rerank_config yields defaults via 'or RerankConfig()' pattern."""
        config = ServerConfig(server_dir=temp_dir)
        # rerank_config is None on a fresh ServerConfig
        rerank = config.rerank_config or RerankConfig()
        assert rerank.voyage_reranker_model == ""
        assert rerank.cohere_reranker_model == ""
        assert rerank.overfetch_multiplier == 5


class TestConfigServiceRerankSettings:
    """AC3: ConfigService dispatch for 'rerank' category."""

    def test_update_rerank_voyage_model(self, config_service):
        """AC3: Can update voyage_reranker_model via update_setting."""
        config_service.update_setting(
            category="rerank",
            key="voyage_reranker_model",
            value="rerank-2",
        )
        config = config_service.get_config()
        assert config.rerank_config is not None
        assert config.rerank_config.voyage_reranker_model == "rerank-2"

    def test_update_rerank_cohere_model(self, config_service):
        """AC3: Can update cohere_reranker_model via update_setting."""
        config_service.update_setting(
            category="rerank",
            key="cohere_reranker_model",
            value="rerank-english-v3.0",
        )
        config = config_service.get_config()
        assert config.rerank_config is not None
        assert config.rerank_config.cohere_reranker_model == "rerank-english-v3.0"

    def test_update_rerank_overfetch_multiplier(self, config_service):
        """AC3: Can update overfetch_multiplier with string-to-int coercion."""
        config_service.update_setting(
            category="rerank",
            key="overfetch_multiplier",
            value="3",
        )
        config = config_service.get_config()
        assert config.rerank_config is not None
        assert config.rerank_config.overfetch_multiplier == 3

    def test_update_rerank_overfetch_multiplier_stored_as_int(self, config_service):
        """AC3: overfetch_multiplier is stored as int after update."""
        config_service.update_setting(
            category="rerank",
            key="overfetch_multiplier",
            value="4",
        )
        assert isinstance(
            config_service.get_config().rerank_config.overfetch_multiplier, int
        )

    def test_update_rerank_unknown_key_raises(self, config_service):
        """AC3: Invalid field name raises ValueError."""
        with pytest.raises(ValueError, match="Unknown rerank setting"):
            config_service.update_setting(
                category="rerank",
                key="nonexistent_field",
                value="something",
            )

    def test_update_rerank_persists_to_disk(self, temp_dir, config_service):
        """AC3: Updated rerank config persists to disk and loads in new service."""
        config_service.update_setting(
            category="rerank",
            key="voyage_reranker_model",
            value="rerank-2",
        )
        new_service = ConfigService(server_dir_path=temp_dir)
        config = new_service.get_config()
        assert config.rerank_config is not None
        assert config.rerank_config.voyage_reranker_model == "rerank-2"

    def test_update_rerank_initializes_rerank_config_if_none(self, config_service):
        """AC3: update_setting creates rerank_config from defaults when it was None."""
        config_service.update_setting(
            category="rerank",
            key="voyage_reranker_model",
            value="rerank-2",
        )
        assert config_service.get_config().rerank_config is not None

    def test_update_rerank_preserves_other_fields(self, config_service):
        """AC3: Updating one rerank field preserves other fields."""
        config_service.update_setting(
            category="rerank",
            key="voyage_reranker_model",
            value="rerank-2",
        )
        config_service.update_setting(
            category="rerank",
            key="overfetch_multiplier",
            value="4",
        )
        config = config_service.get_config()
        assert config.rerank_config.voyage_reranker_model == "rerank-2"
        assert config.rerank_config.overfetch_multiplier == 4

    def test_update_rerank_rejects_overfetch_balanced_key(self, config_service):
        """AC3: overfetch_balanced is no longer a valid key — must raise ValueError."""
        with pytest.raises(ValueError, match="Unknown rerank setting"):
            config_service.update_setting(
                category="rerank",
                key="overfetch_balanced",
                value="2",
            )

    def test_update_rerank_rejects_overfetch_high_key(self, config_service):
        """AC3: overfetch_high is no longer a valid key — must raise ValueError."""
        with pytest.raises(ValueError, match="Unknown rerank setting"):
            config_service.update_setting(
                category="rerank",
                key="overfetch_high",
                value="5",
            )


class TestConfigServiceRerankGetAllSettings:
    """AC3: get_all_settings includes rerank section."""

    def test_get_all_settings_includes_rerank_section(self, config_service):
        """get_all_settings should include rerank section."""
        assert "rerank" in config_service.get_all_settings()

    def test_get_all_settings_rerank_has_all_fields(self, config_service):
        """rerank section should have all three expected fields."""
        rerank = config_service.get_all_settings()["rerank"]
        assert "voyage_reranker_model" in rerank
        assert "cohere_reranker_model" in rerank
        assert "overfetch_multiplier" in rerank

    def test_get_all_settings_rerank_no_overfetch_balanced(self, config_service):
        """rerank section must NOT contain overfetch_balanced."""
        rerank = config_service.get_all_settings()["rerank"]
        assert "overfetch_balanced" not in rerank

    def test_get_all_settings_rerank_no_overfetch_high(self, config_service):
        """rerank section must NOT contain overfetch_high."""
        rerank = config_service.get_all_settings()["rerank"]
        assert "overfetch_high" not in rerank

    def test_get_all_settings_rerank_defaults(self, config_service):
        """rerank section returns correct default values."""
        rerank = config_service.get_all_settings()["rerank"]
        assert rerank["voyage_reranker_model"] == ""
        assert rerank["cohere_reranker_model"] == ""
        assert rerank["overfetch_multiplier"] == 5

    def test_get_all_settings_rerank_reflects_updated_values(self, config_service):
        """get_all_settings should show updated values after update_setting."""
        config_service.update_setting(
            category="rerank",
            key="voyage_reranker_model",
            value="rerank-2",
        )
        assert (
            config_service.get_all_settings()["rerank"]["voyage_reranker_model"]
            == "rerank-2"
        )
