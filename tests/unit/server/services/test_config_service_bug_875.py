"""
Unit tests for Bug #875: Web UI config save failed with ValueError for five
missing keys in _update_claude_cli_setting().

Fields affected:
  - dep_map_fact_check_enabled  (bool)
  - fact_check_timeout_seconds  (int, min 60)
  - scheduled_catchup_enabled   (bool)
  - scheduled_catchup_interval_minutes (int, min 1)
  - cohere_api_key              (Optional[str])

Tests use realistic HTTP form value types (strings) because the Web UI POSTs
form data as strings, not Python natives.
"""

import pytest
from code_indexer.server.services.config_service import ConfigService

# Named constants — no magic numbers in tests
FACT_CHECK_TIMEOUT_VALID = "300"
FACT_CHECK_TIMEOUT_VALID_INT = 300
FACT_CHECK_TIMEOUT_BELOW_MIN = "10"
FACT_CHECK_TIMEOUT_MIN = 60

CATCHUP_INTERVAL_VALID = "120"
CATCHUP_INTERVAL_VALID_INT = 120
CATCHUP_INTERVAL_BELOW_MIN = "0"
CATCHUP_INTERVAL_MIN = 1

MAX_CONCURRENT_VALID = "4"
MAX_CONCURRENT_VALID_INT = 4


@pytest.fixture
def service(tmp_path):
    """Provide a ConfigService backed by a fresh temp directory."""
    return ConfigService(server_dir_path=str(tmp_path))


def _get_claude_config(svc: ConfigService):
    """Retrieve ClaudeIntegrationConfig and assert it is not None."""
    config = svc.get_claude_integration_config()
    assert config is not None
    return config


@pytest.mark.parametrize(
    "field", ["dep_map_fact_check_enabled", "scheduled_catchup_enabled"]
)
def test_bool_fields_accept_true_string(service, field):
    """Both new bool fields accept string 'true' and set the field to True."""
    service.update_setting("claude_cli", field, "true")
    config = _get_claude_config(service)
    assert getattr(config, field) is True


@pytest.mark.parametrize(
    "field", ["dep_map_fact_check_enabled", "scheduled_catchup_enabled"]
)
def test_bool_fields_accept_false_string(service, field):
    """Both new bool fields accept string 'false' and set the field to False."""
    service.update_setting("claude_cli", field, "true")
    service.update_setting("claude_cli", field, "false")
    config = _get_claude_config(service)
    assert getattr(config, field) is False


@pytest.mark.parametrize(
    "field, input_value, expected",
    [
        (
            "fact_check_timeout_seconds",
            FACT_CHECK_TIMEOUT_VALID,
            FACT_CHECK_TIMEOUT_VALID_INT,
        ),
        (
            "scheduled_catchup_interval_minutes",
            CATCHUP_INTERVAL_VALID,
            CATCHUP_INTERVAL_VALID_INT,
        ),
    ],
)
def test_int_fields_accept_valid_string(service, field, input_value, expected):
    """Both new int fields accept a valid string and store the parsed integer."""
    service.update_setting("claude_cli", field, input_value)
    config = _get_claude_config(service)
    assert getattr(config, field) == expected


@pytest.mark.parametrize(
    "field, below_min_value, expected_min",
    [
        (
            "fact_check_timeout_seconds",
            FACT_CHECK_TIMEOUT_BELOW_MIN,
            FACT_CHECK_TIMEOUT_MIN,
        ),
        (
            "scheduled_catchup_interval_minutes",
            CATCHUP_INTERVAL_BELOW_MIN,
            CATCHUP_INTERVAL_MIN,
        ),
    ],
)
def test_int_fields_enforce_minimum(service, field, below_min_value, expected_min):
    """Both new int fields clamp to their declared minimum when given a below-min value."""
    service.update_setting("claude_cli", field, below_min_value)
    config = _get_claude_config(service)
    assert getattr(config, field) == expected_min


def test_cohere_api_key_set_and_clear(service):
    """cohere_api_key stores a non-empty string, then clears to None on empty string."""
    service.update_setting("claude_cli", "cohere_api_key", "co-test-key-123")
    config = _get_claude_config(service)
    assert config.cohere_api_key == "co-test-key-123"

    service.update_setting("claude_cli", "cohere_api_key", "")
    config = _get_claude_config(service)
    assert config.cohere_api_key is None


def test_unknown_key_raises_value_error(service):
    """Unknown keys still raise ValueError — regression guard."""
    with pytest.raises(
        ValueError, match="Unknown claude_cli setting: totally_bogus_key"
    ):
        service.update_setting("claude_cli", "totally_bogus_key", "anything")


def test_existing_key_still_works(service):
    """Regression: already-handled key max_concurrent_claude_cli is unaffected."""
    service.update_setting(
        "claude_cli", "max_concurrent_claude_cli", MAX_CONCURRENT_VALID
    )
    config = _get_claude_config(service)
    assert config.max_concurrent_claude_cli == MAX_CONCURRENT_VALID_INT
