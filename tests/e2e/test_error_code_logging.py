"""
End-to-end tests for error code logging system.

Tests the complete error code logging workflow:
- Error codes in log messages
- Correlation IDs in extra dict
- ERROR_REGISTRY integration
"""

from code_indexer.server.error_codes import ERROR_REGISTRY, get_error_definition
from code_indexer.server.logging_utils import format_error_log, get_log_extra
from code_indexer.server.middleware.correlation import (
    set_correlation_id,
    clear_correlation_id,
)


def test_error_log_format():
    """Test that error logs are formatted correctly with error codes."""
    # Test AUTH-HYBRID-001 error code
    message = format_error_log(
        "AUTH-HYBRID-001", "Hybrid auth (session): user_manager not initialized"
    )

    # Verify format
    assert message.startswith("[AUTH-HYBRID-001]")
    assert "user_manager not initialized" in message

    # Test with additional context
    message = format_error_log("AUTH-HYBRID-002", "User not found", username="testuser")

    assert "[AUTH-HYBRID-002]" in message
    assert "username=testuser" in message


def test_correlation_id_in_logs():
    """Test that correlation_id is included in log extra dict when available."""
    # Set correlation_id using the real API
    set_correlation_id("test-correlation-123")
    try:
        extra = get_log_extra("AUTH-HYBRID-001")

        assert "error_code" in extra
        assert extra["error_code"] == "AUTH-HYBRID-001"
        assert "correlation_id" in extra
        assert extra["correlation_id"] == "test-correlation-123"
    finally:
        clear_correlation_id()

    # Without correlation_id (default state)
    extra = get_log_extra("AUTH-HYBRID-002")

    assert "error_code" in extra
    assert extra["error_code"] == "AUTH-HYBRID-002"
    # correlation_id may or may not be present depending on context


def test_error_registry_integration():
    """Test that ERROR_REGISTRY contains expected error codes and can be queried."""
    # Verify AUTH-HYBRID error codes exist
    assert "AUTH-HYBRID-001" in ERROR_REGISTRY
    assert "AUTH-HYBRID-002" in ERROR_REGISTRY
    assert "AUTH-HYBRID-003" in ERROR_REGISTRY

    # Verify error definitions
    error_001 = get_error_definition("AUTH-HYBRID-001")
    assert error_001 is not None
    assert error_001.code == "AUTH-HYBRID-001"
    assert "user manager" in error_001.description.lower()
    assert error_001.severity.value == "error"

    error_002 = get_error_definition("AUTH-HYBRID-002")
    assert error_002 is not None
    assert "not found" in error_002.description.lower()

    error_003 = get_error_definition("AUTH-HYBRID-003")
    assert error_003 is not None
    assert error_003.severity.value == "warning"

    # Non-existent code returns None
    assert get_error_definition("NONEXISTENT-XXX-999") is None
