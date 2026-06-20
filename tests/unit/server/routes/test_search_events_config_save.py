"""Unit tests for Finding H1: search_event_log section must be in
_VALID_CONFIG_SECTIONS and validated by _validate_config_section (Story #1159).

Before the fix: POST /admin/config/search_event_log returned HTTP 400 "Invalid
section: search_event_log" because "search_event_log" was missing from
_VALID_CONFIG_SECTIONS, making the Web UI save dead for that section.

These tests exercise the two functions directly so no web session/CSRF wiring
is needed, keeping the tests fast and dependency-free.
"""

# ---------------------------------------------------------------------------
# H1.1 - section membership
# ---------------------------------------------------------------------------


class TestSearchEventLogSectionMembership:
    """search_event_log must be present in _VALID_CONFIG_SECTIONS."""

    def test_search_event_log_in_valid_sections(self) -> None:
        from code_indexer.server.web.routes import _VALID_CONFIG_SECTIONS

        assert "search_event_log" in _VALID_CONFIG_SECTIONS, (
            "search_event_log must be listed in _VALID_CONFIG_SECTIONS; "
            "POST /admin/config/search_event_log would otherwise always return "
            "HTTP 400 'Invalid section: search_event_log'."
        )


# ---------------------------------------------------------------------------
# H1.2 - validation logic
# ---------------------------------------------------------------------------


class TestValidateConfigSectionSearchEventLog:
    """_validate_config_section("search_event_log", ...) enforces [1, 3650]."""

    def _validate(self, data: dict):
        from code_indexer.server.web.routes import _validate_config_section

        return _validate_config_section("search_event_log", data)

    def test_valid_retention_days_returns_none(self) -> None:
        assert self._validate({"search_event_log_retention_days": 30}) is None

    def test_boundary_value_1_accepted(self) -> None:
        assert self._validate({"search_event_log_retention_days": 1}) is None

    def test_boundary_value_3650_accepted(self) -> None:
        assert self._validate({"search_event_log_retention_days": 3650}) is None

    def test_string_int_accepted(self) -> None:
        # Form POST sends strings; must coerce cleanly.
        assert self._validate({"search_event_log_retention_days": "365"}) is None

    def test_missing_field_returns_none(self) -> None:
        # Partial save: other keys only — must not error.
        assert self._validate({}) is None

    def test_zero_days_rejected(self) -> None:
        error = self._validate({"search_event_log_retention_days": 0})
        assert error is not None
        assert "1" in error and "3650" in error

    def test_negative_days_rejected(self) -> None:
        error = self._validate({"search_event_log_retention_days": -1})
        assert error is not None

    def test_exceeding_3650_rejected(self) -> None:
        error = self._validate({"search_event_log_retention_days": 3651})
        assert error is not None
        assert "3650" in error

    def test_non_integer_string_rejected(self) -> None:
        error = self._validate({"search_event_log_retention_days": "abc"})
        assert error is not None
        assert "integer" in error.lower()

    def test_float_string_rejected(self) -> None:
        # "30.5" cannot be int()'d cleanly.
        error = self._validate({"search_event_log_retention_days": "30.5"})
        assert error is not None
