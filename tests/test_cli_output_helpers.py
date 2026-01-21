"""Tests for CLI output helpers - Story #735.

Tests the JSON output decorator pattern for consistent CLI output formatting.
"""

import json


class TestJSONOutputFormatting:
    """Tests for JSON output formatting functions."""

    def test_format_json_success_basic_structure(self):
        """Test format_json_success produces correct structure."""
        from code_indexer.cli_utils.output_helpers import format_json_success

        data = {"key": "value", "count": 42}
        result = format_json_success(data)
        parsed = json.loads(result)

        assert parsed["success"] is True
        assert parsed["data"] == data
        assert "metadata" in parsed

    def test_format_json_success_includes_timestamp(self):
        """Test format_json_success includes timestamp in metadata."""
        from code_indexer.cli_utils.output_helpers import format_json_success

        result = format_json_success({"test": "data"})
        parsed = json.loads(result)

        assert "metadata" in parsed
        assert "timestamp" in parsed["metadata"]

    def test_format_json_error_basic_structure(self):
        """Test format_json_error produces correct structure."""
        from code_indexer.cli_utils.output_helpers import format_json_error

        error_message = "Something went wrong"
        error_type = "ValidationError"
        result = format_json_error(error_message, error_type)
        parsed = json.loads(result)

        assert parsed["success"] is False
        assert parsed["error"] == error_message
        assert parsed["error_type"] == error_type

    def test_format_json_error_default_type(self):
        """Test format_json_error uses default error type."""
        from code_indexer.cli_utils.output_helpers import format_json_error

        result = format_json_error("An error occurred")
        parsed = json.loads(result)

        assert parsed["success"] is False
        assert "error_type" in parsed

    def test_json_output_handler_passthrough_non_json(self):
        """Test decorator passes through when json_output=False."""
        from code_indexer.cli_utils.output_helpers import json_output_handler

        @json_output_handler
        def test_command(json_output: bool = False):
            return {"key": "value"}

        result = test_command(json_output=False)
        assert result == {"key": "value"}

    def test_json_output_handler_wraps_result_when_json_true(self):
        """Test decorator wraps result when json_output=True."""
        from code_indexer.cli_utils.output_helpers import json_output_handler

        @json_output_handler
        def test_command(json_output: bool = False):
            return {"key": "value"}

        result = test_command(json_output=True)
        parsed = json.loads(result)
        assert parsed["success"] is True
        assert parsed["data"] == {"key": "value"}

    def test_json_output_handler_handles_exceptions(self):
        """Test decorator catches exceptions and formats as JSON errors."""
        from code_indexer.cli_utils.output_helpers import json_output_handler

        @json_output_handler
        def test_command(json_output: bool = False):
            raise ValueError("Test error")

        result = test_command(json_output=True)
        parsed = json.loads(result)
        assert parsed["success"] is False
        assert "Test error" in parsed["error"]
        assert parsed["error_type"] == "ValueError"
