"""Tests for routes._validate_config_section's search_timeouts branch
covering the new rest_query_handler_timeout_seconds field (Issue #1435).

Mirrors the existing 5-field integer-range validation loop in
_validate_config_section -- see test_search_timeouts_config_1398.py's
TestValidateConfigEnforcesSearchTimeoutsRanges for the config_manager.py
validate_config equivalent of this same range (30-600s).
"""

from code_indexer.server.web.routes import _validate_config_section


class TestValidateConfigSectionRestQueryHandlerTimeout:
    def test_valid_value_passes(self) -> None:
        error = _validate_config_section(
            "search_timeouts", {"rest_query_handler_timeout_seconds": 180}
        )
        assert error is None

    def test_value_below_minimum_rejected(self) -> None:
        error = _validate_config_section(
            "search_timeouts", {"rest_query_handler_timeout_seconds": 29}
        )
        assert error is not None
        assert "30" in error and "600" in error

    def test_value_above_maximum_rejected(self) -> None:
        error = _validate_config_section(
            "search_timeouts", {"rest_query_handler_timeout_seconds": 601}
        )
        assert error is not None
        assert "30" in error and "600" in error

    def test_non_numeric_value_rejected(self) -> None:
        error = _validate_config_section(
            "search_timeouts", {"rest_query_handler_timeout_seconds": "not-a-number"}
        )
        assert error is not None


if __name__ == "__main__":
    import pytest

    pytest.main([__file__, "-v"])
