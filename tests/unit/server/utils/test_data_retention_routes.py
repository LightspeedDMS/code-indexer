"""
Unit tests for route validation of data_retention section (Story #400 - AC4).

Tests _validate_config_section("data_retention", ...) accepts valid data
and rejects out-of-range values, and that the section is in valid_sections.

TDD: These tests are written BEFORE implementation to define expected behavior.
"""


def _validate(data: dict):
    """Helper to call the route validation function for data_retention."""
    from code_indexer.server.web.routes import _validate_config_section

    return _validate_config_section("data_retention", data)


class TestRouteValidationDataRetentionAccepts:
    """AC4: _validate_config_section accepts valid data_retention values."""

    def test_valid_full_data_returns_none(self):
        """Valid data_retention data with all fields should return None (no error)."""
        data = {
            "operational_logs_retention_hours": 168,
            "audit_logs_retention_hours": 2160,
            "sync_jobs_retention_hours": 720,
            "dep_map_history_retention_hours": 2160,
            "background_jobs_retention_hours": 720,
            "cleanup_interval_hours": 1,
        }
        assert _validate(data) is None

    def test_empty_data_returns_none(self):
        """Empty data (no fields to validate) should return None."""
        assert _validate({}) is None

    def test_retention_hours_minimum_valid(self):
        """Retention hours of 1 should be valid."""
        assert _validate({"operational_logs_retention_hours": 1}) is None

    def test_retention_hours_maximum_valid(self):
        """Retention hours of 8760 should be valid."""
        assert _validate({"operational_logs_retention_hours": 8760}) is None

    def test_cleanup_interval_minimum_valid(self):
        """Cleanup interval of 1 should be valid."""
        assert _validate({"cleanup_interval_hours": 1}) is None

    def test_cleanup_interval_maximum_valid(self):
        """Cleanup interval of 24 should be valid."""
        assert _validate({"cleanup_interval_hours": 24}) is None

    def test_each_retention_field_valid_individually(self):
        """Each retention field should be independently valid."""
        fields = [
            "audit_logs_retention_hours",
            "sync_jobs_retention_hours",
            "dep_map_history_retention_hours",
            "background_jobs_retention_hours",
        ]
        for field in fields:
            assert _validate({field: 720}) is None, f"Expected {field}=720 to be valid"


class TestRouteValidationDataRetentionRejects:
    """AC4: _validate_config_section rejects invalid data_retention values."""

    def test_rejects_operational_logs_retention_zero(self):
        """Retention hours of 0 should return error message."""
        result = _validate({"operational_logs_retention_hours": 0})
        assert result is not None

    def test_rejects_operational_logs_retention_too_high(self):
        """Retention hours above 8760 should return error message."""
        result = _validate({"operational_logs_retention_hours": 9000})
        assert result is not None

    def test_rejects_non_numeric_retention_hours(self):
        """Non-numeric retention hours should return error message."""
        result = _validate({"operational_logs_retention_hours": "not_a_number"})
        assert result is not None

    def test_rejects_cleanup_interval_zero(self):
        """Cleanup interval of 0 should return error message."""
        result = _validate({"cleanup_interval_hours": 0})
        assert result is not None

    def test_rejects_cleanup_interval_too_high(self):
        """Cleanup interval above 24 should return error message."""
        result = _validate({"cleanup_interval_hours": 25})
        assert result is not None

    def test_rejects_non_numeric_cleanup_interval(self):
        """Non-numeric cleanup interval should return error message."""
        result = _validate({"cleanup_interval_hours": "not_a_number"})
        assert result is not None

    def test_rejects_audit_logs_retention_zero(self):
        """audit_logs_retention_hours of 0 should return error."""
        assert _validate({"audit_logs_retention_hours": 0}) is not None

    def test_rejects_sync_jobs_retention_zero(self):
        """sync_jobs_retention_hours of 0 should return error."""
        assert _validate({"sync_jobs_retention_hours": 0}) is not None

    def test_rejects_dep_map_history_retention_zero(self):
        """dep_map_history_retention_hours of 0 should return error."""
        assert _validate({"dep_map_history_retention_hours": 0}) is not None

    def test_rejects_background_jobs_retention_zero(self):
        """background_jobs_retention_hours of 0 should return error."""
        assert _validate({"background_jobs_retention_hours": 0}) is not None


class TestRouteValidSectionsContainsDataRetention:
    """AC4: data_retention is in the valid_sections whitelist."""

    def test_data_retention_section_is_handled(self):
        """data_retention section should not return 'Invalid section' error."""
        # The valid_sections list is checked in the route handler, not in
        # _validate_config_section. We verify the validation function handles
        # the section without raising AttributeError or similar unexpected errors.
        result = _validate({"cleanup_interval_hours": 1})
        assert result is None  # Valid data should pass cleanly
