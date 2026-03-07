"""
Unit tests for routes.py validation of cleanup_max_age_hours (Story #360).

Tests that _validate_config_section properly validates the cleanup_max_age_hours
field in the background_jobs section.

TDD: These tests are written BEFORE implementation to define expected behavior.
"""

from code_indexer.server.web.routes import _validate_config_section


class TestRoutesRetentionValidation:
    """Tests for cleanup_max_age_hours validation in _validate_config_section (Story #360)."""

    # ==========================================================================
    # Component 5: Validation in routes.py
    # ==========================================================================

    def test_validates_cleanup_max_age_below_range(self):
        """Story #360: cleanup_max_age_hours below 1 should fail validation."""
        data = {"cleanup_max_age_hours": "0"}
        error = _validate_config_section("background_jobs", data)
        assert error is not None
        assert "cleanup" in error.lower() or "max age" in error.lower() or "1" in error

    def test_validates_cleanup_max_age_above_range(self):
        """Story #360: cleanup_max_age_hours above 8760 should fail validation."""
        data = {"cleanup_max_age_hours": "9000"}
        error = _validate_config_section("background_jobs", data)
        assert error is not None
        assert "8760" in error or "cleanup" in error.lower() or "max age" in error.lower()

    def test_accepts_valid_cleanup_max_age_minimum(self):
        """Story #360: cleanup_max_age_hours of 1 should pass validation."""
        data = {"cleanup_max_age_hours": "1"}
        error = _validate_config_section("background_jobs", data)
        assert error is None

    def test_accepts_valid_cleanup_max_age_default(self):
        """Story #360: cleanup_max_age_hours of 720 (30 days) should pass validation."""
        data = {"cleanup_max_age_hours": "720"}
        error = _validate_config_section("background_jobs", data)
        assert error is None

    def test_accepts_valid_cleanup_max_age_maximum(self):
        """Story #360: cleanup_max_age_hours of 8760 (1 year) should pass validation."""
        data = {"cleanup_max_age_hours": "8760"}
        error = _validate_config_section("background_jobs", data)
        assert error is None

    def test_accepts_missing_cleanup_max_age(self):
        """Story #360: Missing cleanup_max_age_hours should pass (not required)."""
        data = {}
        error = _validate_config_section("background_jobs", data)
        assert error is None

    def test_accepts_none_cleanup_max_age(self):
        """Story #360: None cleanup_max_age_hours should pass (not required)."""
        data = {"cleanup_max_age_hours": None}
        error = _validate_config_section("background_jobs", data)
        assert error is None

    def test_rejects_non_numeric_cleanup_max_age(self):
        """Story #360: Non-numeric cleanup_max_age_hours should fail validation."""
        data = {"cleanup_max_age_hours": "not_a_number"}
        error = _validate_config_section("background_jobs", data)
        assert error is not None
        assert "valid number" in error.lower() or "must be" in error.lower()

    def test_validates_cleanup_max_age_alongside_existing_fields(self):
        """Story #360: Validation should work with existing background_jobs fields."""
        data = {
            "max_concurrent_background_jobs": "5",
            "subprocess_max_workers": "2",
            "cleanup_max_age_hours": "720",
        }
        error = _validate_config_section("background_jobs", data)
        assert error is None

    def test_existing_background_jobs_fields_still_validate(self):
        """Existing background_jobs validation should still work after adding cleanup_max_age_hours."""
        # max_concurrent_background_jobs out of range
        data = {"max_concurrent_background_jobs": "200"}
        error = _validate_config_section("background_jobs", data)
        assert error is not None

        # subprocess_max_workers out of range
        data = {"subprocess_max_workers": "100"}
        error = _validate_config_section("background_jobs", data)
        assert error is not None
