"""
Unit tests for Story #844: ConfigService codex_integration section.

Service-layer tests covering:
  1. get_all_settings exposes a "codex_integration" section
  2. All 6 keys have correct defaults
  3. update_setting roundtrip for all 6 keys persists and re-reads correctly
  4. Invalid credential_mode is rejected with ValueError
  5. api_key masked placeholder is preserved (not wiped)
  6. Unknown key raises ValueError
"""

import pytest

_MASKED_PLACEHOLDER = "dummy-***"


@pytest.fixture
def config_service(tmp_path):
    """ConfigService backed by a temp directory (no real DB entanglement)."""
    from code_indexer.server.services.config_service import ConfigService

    server_dir = tmp_path / "cidx-server"
    server_dir.mkdir()
    return ConfigService(server_dir_path=str(server_dir))


# ---------------------------------------------------------------------------
# 1. Section exists
# ---------------------------------------------------------------------------


def test_codex_integration_section_exists(config_service):
    """get_all_settings must contain a 'codex_integration' section."""
    settings = config_service.get_all_settings()
    assert "codex_integration" in settings


# ---------------------------------------------------------------------------
# 2. Default values (parametrized)
# ---------------------------------------------------------------------------

_DEFAULT_SPECS = [
    ("enabled", False),
    ("credential_mode", "none"),
    ("api_key", None),
    ("lcp_url", None),
    ("lcp_vendor", "openai"),
    ("codex_weight", 0.5),
]


@pytest.mark.parametrize(
    "key,default", _DEFAULT_SPECS, ids=[s[0] for s in _DEFAULT_SPECS]
)
def test_codex_integration_default(config_service, key, default):
    """Each key's default value must be exposed in get_all_settings."""
    settings = config_service.get_all_settings()
    result = settings["codex_integration"][key]
    if isinstance(default, float):
        assert result == pytest.approx(default)
    else:
        assert result == default


# ---------------------------------------------------------------------------
# 3. update_setting roundtrip (all 6 keys parametrized)
# ---------------------------------------------------------------------------

_UPDATE_SPECS = [
    ("enabled", True, bool),
    ("credential_mode", "api_key", str),
    # api_key excluded: get_all_settings() masks it (first 6 chars + "***"),
    # so a generic roundtrip cannot compare the stored vs returned values.
    # api_key behaviour is covered by test_codex_api_key_stored_and_returned_masked
    # and test_api_key_masked_placeholder_preserved below.
    ("lcp_url", "https://example.invalid/lcp", str),
    ("lcp_vendor", "azure", str),
    ("codex_weight", 0.8, float),
]


@pytest.mark.parametrize(
    "key,new_value,expected_type", _UPDATE_SPECS, ids=[s[0] for s in _UPDATE_SPECS]
)
def test_codex_integration_update_roundtrip(
    config_service, key, new_value, expected_type
):
    """update_setting roundtrip: each key can be set and immediately read back."""
    config_service.update_setting("codex_integration", key, new_value)
    settings = config_service.get_all_settings()
    result = settings["codex_integration"][key]
    if expected_type is float:
        assert result == pytest.approx(new_value)
    else:
        assert result == new_value
    assert isinstance(result, expected_type)


# ---------------------------------------------------------------------------
# 3b. api_key masking: get_all_settings returns masked form
# ---------------------------------------------------------------------------


def test_codex_api_key_stored_and_returned_masked(config_service):
    """get_all_settings() must return a masked api_key (first 6 chars + '***'),
    not the raw stored value."""
    config_service.update_setting(
        "codex_integration", "api_key", "dummy-api-key-not-real"
    )
    settings = config_service.get_all_settings()
    assert settings["codex_integration"]["api_key"] == "dummy-***", (
        "Expected masked api_key 'dummy-***' from get_all_settings(), "
        f"got: {settings['codex_integration']['api_key']!r}"
    )


# ---------------------------------------------------------------------------
# 4. Validation: invalid credential_mode rejected
# ---------------------------------------------------------------------------


def test_invalid_credential_mode_rejected(config_service):
    """update_setting with invalid credential_mode must raise ValueError."""
    with pytest.raises(ValueError):
        config_service.update_setting("codex_integration", "credential_mode", "invalid")


# ---------------------------------------------------------------------------
# 5. api_key masked placeholder preservation
# ---------------------------------------------------------------------------


def test_api_key_masked_placeholder_preserved(config_service):
    """When the submitted api_key contains '***' (masked placeholder),
    the existing stored value must be preserved — not overwritten with the mask."""
    # First store a real (dummy) key
    config_service.update_setting("codex_integration", "api_key", "dummy-real-key")
    # Now submit a masked placeholder (as the UI would when re-saving without editing)
    config_service.update_setting("codex_integration", "api_key", _MASKED_PLACEHOLDER)
    # The raw stored key must still be "dummy-real-key", not the placeholder
    config = config_service.get_config()
    assert config.codex_integration_config is not None
    stored_key = config.codex_integration_config.api_key
    assert stored_key == "dummy-real-key", (
        f"Masked placeholder overwrote the real key. Got: {stored_key!r}"
    )


# ---------------------------------------------------------------------------
# 6. Unknown key raises ValueError
# ---------------------------------------------------------------------------


def test_unknown_key_raises_value_error(config_service):
    """update_setting with unknown key must raise ValueError."""
    with pytest.raises(ValueError, match="Unknown codex_integration setting"):
        config_service.update_setting("codex_integration", "nonexistent_key", "value")
