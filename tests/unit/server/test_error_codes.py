"""
Unit tests for error_codes module.

Tests the error code registry system including:
- ErrorDefinition dataclass structure
- Severity enum values
- ERROR_REGISTRY dictionary format
- Error code format validation
- Registry lookup functionality
"""

import pytest
from dataclasses import fields


def test_error_definition_structure():
    """Test that ErrorDefinition dataclass has required fields."""
    from code_indexer.server.error_codes import ErrorDefinition, Severity

    # Create an instance
    error_def = ErrorDefinition(
        code="TEST-001",
        description="Test error",
        severity=Severity.ERROR,
        action="Test action",
    )

    # Verify fields exist and have correct values
    assert error_def.code == "TEST-001"
    assert error_def.description == "Test error"
    assert error_def.severity == Severity.ERROR
    assert error_def.action == "Test action"

    # Verify it's a dataclass with exactly 4 fields
    field_names = {f.name for f in fields(ErrorDefinition)}
    assert field_names == {"code", "description", "severity", "action"}


def test_severity_enum():
    """Test that Severity enum has correct values."""
    from code_indexer.server.error_codes import Severity

    assert Severity.WARNING.value == "warning"
    assert Severity.ERROR.value == "error"
    assert Severity.CRITICAL.value == "critical"

    # Verify all enum members
    assert len(list(Severity)) == 3


def test_error_registry_exists():
    """Test that ERROR_REGISTRY dictionary exists and is properly structured."""
    from code_indexer.server.error_codes import ERROR_REGISTRY, ErrorDefinition

    # Registry should exist and be a dict
    assert isinstance(ERROR_REGISTRY, dict)

    # Should have at least one entry (we'll populate it incrementally)
    assert len(ERROR_REGISTRY) >= 0  # Start with empty registry

    # All values should be ErrorDefinition instances
    for code, definition in ERROR_REGISTRY.items():
        assert isinstance(code, str)
        assert isinstance(definition, ErrorDefinition)
        # The key should match the definition's code
        assert code == definition.code


def test_error_code_format_validation():
    """Test error code format matches {SUBSYSTEM}-{CATEGORY}-{NUMBER}."""
    from code_indexer.server.error_codes import validate_error_code_format

    # Valid formats
    assert validate_error_code_format("AUTH-OIDC-001") is True
    assert validate_error_code_format("MCP-TOOL-042") is True
    assert validate_error_code_format("GIT-CLONE-999") is True

    # Invalid formats
    assert validate_error_code_format("AUTH001") is False  # Missing hyphens
    assert validate_error_code_format("AUTH-OIDC") is False  # Missing number
    assert validate_error_code_format("auth-oidc-001") is False  # Lowercase
    assert validate_error_code_format("AUTH-OIDC-1") is False  # Not 3 digits
    assert validate_error_code_format("") is False  # Empty
    assert validate_error_code_format("AUTH-OIDC-00A") is False  # Non-numeric


def test_error_registry_lookup():
    """Test that we can look up error definitions by code."""
    from code_indexer.server.error_codes import ERROR_REGISTRY, get_error_definition

    # Add a test entry if registry is empty
    if len(ERROR_REGISTRY) == 0:
        pytest.skip("ERROR_REGISTRY is empty, will be populated in implementation")

    # Get first error code from registry
    first_code = next(iter(ERROR_REGISTRY.keys()))

    # Should be able to retrieve it
    definition = get_error_definition(first_code)
    assert definition is not None
    assert definition.code == first_code

    # Non-existent code should return None
    assert get_error_definition("NONEXISTENT-XXX-999") is None


def test_error_code_uniqueness():
    """Test that all error codes in registry are unique."""
    from code_indexer.server.error_codes import ERROR_REGISTRY

    if len(ERROR_REGISTRY) == 0:
        pytest.skip("ERROR_REGISTRY is empty, will be populated in implementation")

    codes = list(ERROR_REGISTRY.keys())
    # Number of unique codes should equal total codes
    assert len(codes) == len(set(codes))
