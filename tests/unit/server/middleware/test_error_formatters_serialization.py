"""
Test error formatter JSON serialization edge cases.

Tests Bug #152 fix: _serialize_value_for_json() handling of bytes and callable types.
Tests Bug #1003 fix: create_validation_error_response() bytes in rejected_value path.
"""

import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi.exceptions import RequestValidationError

from src.code_indexer.server.middleware.error_formatters import (
    _serialize_value_for_json,
    create_validation_error_response,
    generate_correlation_id,
    get_current_timestamp,
)


class _PassthroughSanitizer:
    """Minimal sanitizer that returns values unchanged for testing."""

    def sanitize_field_value(self, field_name, value):
        return value

    def sanitize_string(self, s):
        return s


class TestSerializeValueForJson:
    """Test _serialize_value_for_json() edge cases."""

    def test_serialize_bytes_value(self):
        """Test serialization of bytes objects."""
        # Given: A bytes object
        test_bytes = b"Hello World"

        # When: Serializing it
        result = _serialize_value_for_json(test_bytes)

        # Then: Should return a string representation
        assert isinstance(result, str)
        assert result == "<bytes:11 bytes>"

    def test_serialize_empty_bytes(self):
        """Test serialization of empty bytes object."""
        # Given: An empty bytes object
        test_bytes = b""

        # When: Serializing it
        result = _serialize_value_for_json(test_bytes)

        # Then: Should return a string representation
        assert isinstance(result, str)
        assert result == "<bytes:0 bytes>"

    def test_serialize_callable_with_name(self):
        """Test serialization of callable with __name__ attribute."""

        # Given: A function
        def test_function():
            pass

        # When: Serializing it
        result = _serialize_value_for_json(test_function)

        # Then: Should return a string representation with function name
        assert isinstance(result, str)
        assert result == "<function:test_function>"

    def test_serialize_lambda_callable(self):
        """Test serialization of lambda (callable without proper __name__)."""
        # Given: A lambda function
        test_lambda = lambda x: x  # noqa: E731

        # When: Serializing it
        result = _serialize_value_for_json(test_lambda)

        # Then: Should return a string representation
        assert isinstance(result, str)
        assert "<function:" in result

    def test_serialize_nested_dict_with_bytes(self):
        """Test serialization of nested dict containing bytes."""
        # Given: A nested dict with bytes
        test_dict = {"normal": "value", "nested": {"data": b"binary data", "count": 42}}

        # When: Serializing it
        result = _serialize_value_for_json(test_dict)

        # Then: Should recursively serialize bytes
        assert result == {
            "normal": "value",
            "nested": {"data": "<bytes:11 bytes>", "count": 42},
        }

    def test_serialize_list_with_callable(self):
        """Test serialization of list containing callable."""

        # Given: A list with callable
        def sample_func():
            pass

        test_list = ["item1", sample_func, "item3"]

        # When: Serializing it
        result = _serialize_value_for_json(test_list)

        # Then: Should recursively serialize callable
        assert result == ["item1", "<function:sample_func>", "item3"]

    def test_serialize_existing_types_still_work(self):
        """Test that existing serialization for datetime and Path still works."""
        # Given: datetime and Path objects
        test_datetime = datetime(2025, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        test_path = Path("/some/path")

        # When: Serializing them
        datetime_result = _serialize_value_for_json(test_datetime)
        path_result = _serialize_value_for_json(test_path)

        # Then: Should use existing serialization logic
        assert datetime_result == "2025-01-01T12:00:00+00:00"
        assert path_result == "/some/path"

    def test_serialize_complex_nested_structure(self):
        """Test serialization of complex structure with multiple edge case types."""

        # Given: A complex structure with various types
        def my_callback():
            pass

        test_structure = {
            "timestamp": datetime(2025, 1, 1, tzinfo=timezone.utc),
            "file": Path("/test.txt"),
            "data": b"binary",
            "handler": my_callback,
            "nested": [{"inner_bytes": b"test", "inner_func": len}],
        }

        # When: Serializing it
        result = _serialize_value_for_json(test_structure)

        # Then: Should handle all types correctly
        assert result == {
            "timestamp": "2025-01-01T00:00:00+00:00",
            "file": "/test.txt",
            "data": "<bytes:6 bytes>",
            "handler": "<function:my_callback>",
            "nested": [
                {"inner_bytes": "<bytes:4 bytes>", "inner_func": "<function:len>"}
            ],
        }


class TestCreateValidationErrorResponseSerialization:
    """Bug #1003 regression: bytes in rejected_value must be JSON-serializable."""

    def test_bytes_rejected_value_serialized_in_validation_response(self):
        """Bytes input from malformed request body must not crash JSON serialization."""
        errors = [
            {
                "type": "string_type",
                "loc": ("body", "payload"),
                "msg": "Input should be a valid string",
                "input": b"\x89PNG\r\n\x1a\n\x00",
            }
        ]
        exc = RequestValidationError(errors=errors)

        result = create_validation_error_response(
            validation_error=exc,
            sanitizer=_PassthroughSanitizer(),
            correlation_id=generate_correlation_id(),
            timestamp=get_current_timestamp(),
        )

        # Must be JSON-serializable without TypeError
        serialized = json.dumps(result)
        assert serialized is not None

        # The bytes value should appear as "<bytes:N bytes>" in field_errors
        field_errors = result["details"]["field_errors"]
        assert len(field_errors) == 1
        assert "<bytes:" in str(field_errors[0]["rejected_value"])
