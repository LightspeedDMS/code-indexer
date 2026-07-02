"""
Unit tests for Story #724 Phase C: post-generation verification pass config fields
in the Web UI Config Screen.

Tests cover:
- _get_current_config populates dep_map_fact_check_enabled and
  fact_check_timeout_seconds with correct defaults (absent, empty, None claude_cli).
- _get_current_config preserves stored values both individually and together.
- _validate_config_section enforces bounds on fact_check_timeout_seconds,
  including boundary cases just below min and just above max.
- _validate_config_section accepts a POST without fact_check_timeout_seconds.
- Template HTML contains both new field name= attributes, min/max HTML attributes,
  and the Post-Generation Verification sub-header — all in a single parametrized test.
"""

import pytest
from pathlib import Path
from unittest.mock import Mock, patch

from code_indexer.server.web.routes import _get_current_config, _validate_config_section

# ---------------------------------------------------------------------------
# Named constants — no raw numeric or index literals anywhere in this file
# ---------------------------------------------------------------------------

# Verification timeout bounds (Story #724)
MIN_FACT_CHECK_TIMEOUT = 60
DEFAULT_FACT_CHECK_TIMEOUT = 600
MAX_FACT_CHECK_TIMEOUT = 3600
CUSTOM_FACT_CHECK_TIMEOUT = 1200  # arbitrary valid value for preservation tests
BELOW_MIN_TIMEOUT = 5
JUST_BELOW_MIN_TIMEOUT = 59
JUST_ABOVE_MAX_TIMEOUT = 3601
ABOVE_MAX_TIMEOUT = 99999

# Template scan window and index sentinels
HTML_CONTEXT_WINDOW = 200
NOT_FOUND_INDEX = -1
SLICE_START = 0

# Minimal server settings used by _base_settings
_TEST_SERVER_PORT = 8000
_TEST_CACHE_TTL = 300
_TEST_TIMEOUT_DEFAULT = 30
_TEST_PASSWORD_MIN_LENGTH = 8

# Sentinel: omit the claude_cli key entirely from settings
_SENTINEL_MISSING = "__missing__"

# Template string fragments checked in the unified template test
_FRAGMENT_FACT_CHECK_ENABLED_NAME = 'name="dep_map_fact_check_enabled"'
_FRAGMENT_FACT_CHECK_TIMEOUT_NAME = 'name="fact_check_timeout_seconds"'
_FRAGMENT_SUBHEADER = "Post-Generation Verification"
_FRAGMENT_TIMEOUT_MIN = f'min="{MIN_FACT_CHECK_TIMEOUT}"'
_FRAGMENT_TIMEOUT_MAX = f'max="{MAX_FACT_CHECK_TIMEOUT}"'


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _base_settings(claude_cli_value=_SENTINEL_MISSING):
    """Minimal settings dict. Use _SENTINEL_MISSING to omit claude_cli key entirely."""
    settings = {
        "server": {"host": "0.0.0.0", "port": _TEST_SERVER_PORT},
        "cache": {"ttl_seconds": _TEST_CACHE_TTL},
        "timeouts": {"default": _TEST_TIMEOUT_DEFAULT},
        "password_security": {"min_length": _TEST_PASSWORD_MIN_LENGTH},
    }
    if claude_cli_value != _SENTINEL_MISSING:
        settings["claude_cli"] = claude_cli_value
    return settings


def _call_get_current_config(settings):
    """Patch get_config_service and call _get_current_config."""
    with patch(
        "code_indexer.server.services.config_service.get_config_service"
    ) as mock_service:
        mock_cs = Mock()
        mock_cs.get_all_settings.return_value = settings
        mock_service.return_value = mock_cs
        return _get_current_config()


def _get_timeout_input_context(html):
    """Return the HTML context window centred on the fact_check_timeout_seconds input."""
    idx = html.find(_FRAGMENT_FACT_CHECK_TIMEOUT_NAME)
    assert idx != NOT_FOUND_INDEX, (
        f"{_FRAGMENT_FACT_CHECK_TIMEOUT_NAME} not found in template"
    )
    return html[max(SLICE_START, idx - HTML_CONTEXT_WINDOW) : idx + HTML_CONTEXT_WINDOW]


@pytest.fixture(scope="module")
def template_html():
    """Read config_section.html once for the whole module."""
    template_dir = (
        Path(__file__).parent.parent.parent.parent.parent
        / "src"
        / "code_indexer"
        / "server"
        / "web"
        / "templates"
    )
    return (template_dir / "partials" / "config_section.html").read_text()


# ---------------------------------------------------------------------------
# _get_current_config: default population
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "claude_cli_input",
    [
        pytest.param(_SENTINEL_MISSING, id="key_absent"),
        pytest.param(None, id="value_none"),
        pytest.param({}, id="empty_dict"),
    ],
)
class TestConfigPageVerificationPassDefaults:
    """_get_current_config provides correct defaults when claude_cli is not set."""

    def test_dep_map_fact_check_enabled_defaults_to_false(self, claude_cli_input):
        settings = _base_settings(claude_cli_input)
        config = _call_get_current_config(settings)
        assert config["claude_cli"]["dep_map_fact_check_enabled"] is False

    def test_fact_check_timeout_seconds_defaults_to_default(self, claude_cli_input):
        settings = _base_settings(claude_cli_input)
        config = _call_get_current_config(settings)
        assert (
            config["claude_cli"]["fact_check_timeout_seconds"]
            == DEFAULT_FACT_CHECK_TIMEOUT
        )


# ---------------------------------------------------------------------------
# _get_current_config: preservation of stored values
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "field, stored_value, expected",
    [
        pytest.param(
            "dep_map_fact_check_enabled", True, True, id="fact_check_enabled_true"
        ),
        pytest.param(
            "dep_map_fact_check_enabled", False, False, id="fact_check_enabled_false"
        ),
        pytest.param(
            "fact_check_timeout_seconds",
            CUSTOM_FACT_CHECK_TIMEOUT,
            CUSTOM_FACT_CHECK_TIMEOUT,
            id="custom_timeout",
        ),
        pytest.param(
            "fact_check_timeout_seconds",
            MIN_FACT_CHECK_TIMEOUT,
            MIN_FACT_CHECK_TIMEOUT,
            id="timeout_at_minimum",
        ),
    ],
)
def test_stored_value_is_preserved(field, stored_value, expected):
    """_get_current_config preserves individual stored field values."""
    settings = _base_settings({field: stored_value})
    config = _call_get_current_config(settings)
    assert config["claude_cli"][field] == expected


def test_both_stored_values_preserved_together():
    """_get_current_config preserves both new fields simultaneously when both are stored."""
    settings = _base_settings(
        {
            "dep_map_fact_check_enabled": True,
            "fact_check_timeout_seconds": CUSTOM_FACT_CHECK_TIMEOUT,
        }
    )
    config = _call_get_current_config(settings)
    assert config["claude_cli"]["dep_map_fact_check_enabled"] is True
    assert (
        config["claude_cli"]["fact_check_timeout_seconds"] == CUSTOM_FACT_CHECK_TIMEOUT
    )


# ---------------------------------------------------------------------------
# _validate_config_section: claude_cli bounds enforcement
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "timeout_value",
    [
        pytest.param(str(MIN_FACT_CHECK_TIMEOUT), id="minimum"),
        pytest.param(str(DEFAULT_FACT_CHECK_TIMEOUT), id="default"),
        pytest.param(str(MAX_FACT_CHECK_TIMEOUT), id="maximum"),
    ],
)
def test_valid_fact_check_timeout_passes(timeout_value):
    """Valid fact_check_timeout_seconds values at min/default/max pass validation."""
    error = _validate_config_section(
        "claude_cli", {"fact_check_timeout_seconds": timeout_value}
    )
    assert error is None


@pytest.mark.parametrize(
    "timeout_value",
    [
        pytest.param(str(BELOW_MIN_TIMEOUT), id="well_below"),
        pytest.param(str(JUST_BELOW_MIN_TIMEOUT), id="boundary_just_below_min"),
        pytest.param(str(JUST_ABOVE_MAX_TIMEOUT), id="boundary_just_above_max"),
        pytest.param(str(ABOVE_MAX_TIMEOUT), id="well_above"),
    ],
)
def test_out_of_range_fact_check_timeout_rejected(timeout_value):
    """Out-of-range values are rejected; error message includes both bounds."""
    error = _validate_config_section(
        "claude_cli", {"fact_check_timeout_seconds": timeout_value}
    )
    assert error is not None
    assert str(MIN_FACT_CHECK_TIMEOUT) in error
    assert str(MAX_FACT_CHECK_TIMEOUT) in error


def test_non_numeric_fact_check_timeout_rejected():
    """Non-numeric fact_check_timeout_seconds is rejected."""
    error = _validate_config_section(
        "claude_cli", {"fact_check_timeout_seconds": "abc"}
    )
    assert error is not None
    assert "valid number" in error.lower() or "must be" in error.lower()


def test_absent_timeout_field_passes():
    """POST without fact_check_timeout_seconds (field absent) passes validation."""
    assert _validate_config_section("claude_cli", {}) is None


def test_fact_check_enabled_only_passes():
    """POST with only dep_map_fact_check_enabled (no timeout) passes validation."""
    assert (
        _validate_config_section("claude_cli", {"dep_map_fact_check_enabled": "true"})
        is None
    )


# ---------------------------------------------------------------------------
# Template HTML: all new field widgets in one parametrized test
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "fragment, requires_timeout_context",
    [
        pytest.param(
            _FRAGMENT_FACT_CHECK_ENABLED_NAME, False, id="fact_check_enabled_name"
        ),
        pytest.param(
            _FRAGMENT_FACT_CHECK_TIMEOUT_NAME, False, id="fact_check_timeout_name"
        ),
        pytest.param(_FRAGMENT_SUBHEADER, False, id="subheader"),
        pytest.param(_FRAGMENT_TIMEOUT_MIN, True, id="min_attribute"),
        pytest.param(_FRAGMENT_TIMEOUT_MAX, True, id="max_attribute"),
    ],
)
def test_template_contains_verification_pass_fragment(
    template_html, fragment, requires_timeout_context
):
    """config_section.html contains each expected Story #724 HTML fragment.

    For min/max attributes, check within the context window of the timeout input
    to avoid false positives from other numeric inputs in the template.
    """
    if requires_timeout_context:
        search_scope = _get_timeout_input_context(template_html)
    else:
        search_scope = template_html
    assert fragment in search_scope, (
        f"Expected fragment not found in template: {fragment!r}"
    )
