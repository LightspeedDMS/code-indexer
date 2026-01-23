"""
Unit tests for ApiKeyValidator - API key format validation.

Tests cover:
- Anthropic API key format validation (sk-ant-* prefix, minimum length)
- VoyageAI API key format validation (pa-* prefix, minimum length)
- Edge cases (empty, None, invalid formats)

Story #20: API Key Management for Claude CLI and VoyageAI
"""

import pytest

from code_indexer.server.services.api_key_management import (
    ApiKeyValidator,
    ValidationResult,
)


class TestAnthropicApiKeyValidation:
    """Test Anthropic API key format validation."""

    def test_valid_anthropic_api_key(self):
        """AC: Valid Anthropic API key starting with 'sk-ant-' passes validation."""
        # Typical Anthropic API key format
        api_key = "sk-ant-api03-abcdefghijklmnopqrstuvwxyz123456789012345678901234567890"
        result = ApiKeyValidator.validate_anthropic_format(api_key)

        assert result.valid is True
        assert result.error is None

    def test_valid_anthropic_api_key_minimum_length(self):
        """AC: Anthropic API key with minimum required length passes."""
        # Key exactly 40 characters long (minimum)
        api_key = "sk-ant-" + "a" * 33  # 7 + 33 = 40 chars
        result = ApiKeyValidator.validate_anthropic_format(api_key)

        assert result.valid is True
        assert result.error is None

    def test_invalid_anthropic_api_key_wrong_prefix(self):
        """AC: Anthropic API key without 'sk-ant-' prefix fails validation."""
        api_key = "sk-wrong-api03-abcdefghijklmnopqrstuvwxyz123456"
        result = ApiKeyValidator.validate_anthropic_format(api_key)

        assert result.valid is False
        assert "sk-ant-" in result.error

    def test_invalid_anthropic_api_key_too_short(self):
        """AC: Anthropic API key shorter than 40 characters fails validation."""
        api_key = "sk-ant-short"  # Only 12 characters
        result = ApiKeyValidator.validate_anthropic_format(api_key)

        assert result.valid is False
        assert "too short" in result.error.lower()

    def test_invalid_anthropic_api_key_empty_string(self):
        """AC: Empty string API key fails validation."""
        api_key = ""
        result = ApiKeyValidator.validate_anthropic_format(api_key)

        assert result.valid is False
        assert "required" in result.error.lower()

    def test_invalid_anthropic_api_key_none(self):
        """AC: None API key fails validation."""
        api_key = None
        result = ApiKeyValidator.validate_anthropic_format(api_key)

        assert result.valid is False
        assert "required" in result.error.lower()

    def test_invalid_anthropic_api_key_whitespace_only(self):
        """Whitespace-only API key fails validation."""
        api_key = "   "
        result = ApiKeyValidator.validate_anthropic_format(api_key)

        assert result.valid is False
        assert "required" in result.error.lower()


class TestVoyageAIApiKeyValidation:
    """Test VoyageAI API key format validation."""

    def test_valid_voyageai_api_key(self):
        """AC: Valid VoyageAI API key starting with 'pa-' passes validation."""
        # Typical VoyageAI API key format
        api_key = "pa-abcdefghijklmnopqrstuvwxyz"
        result = ApiKeyValidator.validate_voyageai_format(api_key)

        assert result.valid is True
        assert result.error is None

    def test_valid_voyageai_api_key_minimum_length(self):
        """AC: VoyageAI API key with minimum required length passes."""
        # Key exactly 20 characters long (minimum)
        api_key = "pa-" + "a" * 17  # 3 + 17 = 20 chars
        result = ApiKeyValidator.validate_voyageai_format(api_key)

        assert result.valid is True
        assert result.error is None

    def test_invalid_voyageai_api_key_wrong_prefix(self):
        """AC: VoyageAI API key without 'pa-' prefix fails validation."""
        api_key = "pk-abcdefghijklmnopqrstuvwxyz"
        result = ApiKeyValidator.validate_voyageai_format(api_key)

        assert result.valid is False
        assert "pa-" in result.error

    def test_invalid_voyageai_api_key_too_short(self):
        """AC: VoyageAI API key shorter than 20 characters fails validation."""
        api_key = "pa-short"  # Only 8 characters
        result = ApiKeyValidator.validate_voyageai_format(api_key)

        assert result.valid is False
        assert "too short" in result.error.lower()

    def test_invalid_voyageai_api_key_empty_string(self):
        """AC: Empty string API key fails validation."""
        api_key = ""
        result = ApiKeyValidator.validate_voyageai_format(api_key)

        assert result.valid is False
        assert "required" in result.error.lower()

    def test_invalid_voyageai_api_key_none(self):
        """AC: None API key fails validation."""
        api_key = None
        result = ApiKeyValidator.validate_voyageai_format(api_key)

        assert result.valid is False
        assert "required" in result.error.lower()

    def test_invalid_voyageai_api_key_whitespace_only(self):
        """Whitespace-only API key fails validation."""
        api_key = "   "
        result = ApiKeyValidator.validate_voyageai_format(api_key)

        assert result.valid is False
        assert "required" in result.error.lower()


class TestValidationResultDataClass:
    """Test ValidationResult data class properties."""

    def test_validation_result_valid_state(self):
        """ValidationResult with valid=True has None error."""
        result = ValidationResult(valid=True)
        assert result.valid is True
        assert result.error is None

    def test_validation_result_invalid_state(self):
        """ValidationResult with valid=False includes error message."""
        result = ValidationResult(valid=False, error="Test error message")
        assert result.valid is False
        assert result.error == "Test error message"

    def test_validation_result_is_immutable_dataclass(self):
        """ValidationResult should be a frozen dataclass for immutability."""
        result = ValidationResult(valid=True)
        # Should raise error if trying to modify (frozen=True)
        with pytest.raises(AttributeError):
            result.valid = False
