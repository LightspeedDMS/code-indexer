"""
Unit tests for route validation of rerank section (Story #652 - AC4).

Tests _validate_config_section("rerank", ...) accepts valid data
and rejects invalid values, and that the section is in valid_sections.

TDD: These tests are written BEFORE implementation to define expected behavior.
"""

import pytest

from code_indexer.server.services.config_service import ConfigService


@pytest.fixture
def rerank_settings(tmp_path):
    """Provide get_all_settings() output from a fresh ConfigService."""
    service = ConfigService(server_dir_path=str(tmp_path))
    return service.get_all_settings()


def _validate(data: dict):
    """Helper to call the route validation function for rerank."""
    from code_indexer.server.web.routes import _validate_config_section

    return _validate_config_section("rerank", data)


class TestRouteValidationRerankAccepts:
    """AC4: _validate_config_section accepts valid rerank values."""

    def test_valid_full_data_returns_none(self):
        """Valid rerank data with all fields should return None (no error)."""
        data = {
            "voyage_reranker_model": "rerank-2.5",
            "cohere_reranker_model": "rerank-english-v3.0",
            "overfetch_multiplier": "5",
        }
        assert _validate(data) is None

    def test_empty_data_returns_none(self):
        """Empty data (no fields to validate) should return None."""
        assert _validate({}) is None

    def test_empty_model_strings_valid(self):
        """Empty model strings (disabled state) should be valid."""
        data = {
            "voyage_reranker_model": "",
            "cohere_reranker_model": "",
        }
        assert _validate(data) is None

    def test_overfetch_multiplier_minimum_valid(self):
        """overfetch_multiplier of 1 should be valid."""
        assert _validate({"overfetch_multiplier": "1"}) is None

    def test_overfetch_multiplier_large_value_valid(self):
        """overfetch_multiplier of 20 should be valid."""
        assert _validate({"overfetch_multiplier": "20"}) is None

    def test_model_only_data_valid(self):
        """Providing only model fields without overfetch should be valid."""
        assert _validate({"voyage_reranker_model": "rerank-2.5"}) is None

    def test_overfetch_only_data_valid(self):
        """Providing only overfetch_multiplier should be valid."""
        assert _validate({"overfetch_multiplier": "5"}) is None

    def test_overfetch_multiplier_default_value_valid(self):
        """overfetch_multiplier of 5 (the default) should be valid."""
        assert _validate({"overfetch_multiplier": "5"}) is None


class TestRouteValidationRerankRejects:
    """AC4: _validate_config_section rejects invalid rerank values."""

    def test_rejects_overfetch_multiplier_zero(self):
        """overfetch_multiplier of 0 should return error message."""
        result = _validate({"overfetch_multiplier": "0"})
        assert result is not None

    def test_rejects_overfetch_multiplier_non_numeric(self):
        """Non-numeric overfetch_multiplier should return error message."""
        result = _validate({"overfetch_multiplier": "not_a_number"})
        assert result is not None

    def test_rejects_overfetch_multiplier_negative(self):
        """Negative overfetch_multiplier should return error message."""
        result = _validate({"overfetch_multiplier": "-1"})
        assert result is not None

    def test_error_message_mentions_overfetch_multiplier(self):
        """Error for overfetch_multiplier should mention the field."""
        result = _validate({"overfetch_multiplier": "0"})
        assert result is not None
        assert "overfetch" in result.lower() or "multiplier" in result.lower()


class TestRouteValidSectionsContainsRerank:
    """AC4: rerank is in the valid_sections whitelist."""

    def test_rerank_section_is_valid(self):
        """rerank section must be in the valid_sections list in the route handler."""
        result = _validate({"overfetch_multiplier": "5"})
        assert result is None  # Valid data should pass cleanly

    def test_rerank_section_not_rejected_as_invalid(self):
        """Passing rerank section with empty data should not error (section is recognized)."""
        assert _validate({}) is None


class TestGetCurrentConfigIncludesRerank:
    """AC4: _get_current_config() includes rerank key for template rendering."""

    def test_get_current_config_includes_rerank_key(self, rerank_settings):
        """_get_current_config() should include a 'rerank' key."""
        assert "rerank" in rerank_settings, (
            "_get_current_config must include 'rerank' key"
        )

    def test_get_current_config_rerank_has_expected_fields(self, rerank_settings):
        """_get_current_config() rerank section has all 3 fields, no legacy fields."""
        rerank = rerank_settings.get("rerank", {})
        assert "voyage_reranker_model" in rerank
        assert "cohere_reranker_model" in rerank
        assert "overfetch_multiplier" in rerank
        assert "overfetch_balanced" not in rerank
        assert "overfetch_high" not in rerank
