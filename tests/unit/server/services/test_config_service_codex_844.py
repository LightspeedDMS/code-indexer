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
    # api_key excluded: get_all_settings() masks it (last 4 chars only),
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
    """get_all_settings() must return a masked api_key revealing at most the
    last 4 characters (finding 058), not the raw stored value."""
    config_service.update_setting(
        "codex_integration", "api_key", "dummy-api-key-not-real"
    )
    settings = config_service.get_all_settings()
    assert settings["codex_integration"]["api_key"] == "•" * 8 + "real", (
        "Expected masked api_key (8 bullets + last 4) from get_all_settings(), "
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


def _stored_codex_key(config_service):
    config = config_service.get_config()
    assert config.codex_integration_config is not None
    return config.codex_integration_config.api_key


@pytest.mark.parametrize(
    "stored_key",
    ["dummy-real-key-not-real-0000", "dummy-short"],
    ids=["masked-tail", "configured"],
)
def test_api_key_display_mask_preserved(config_service, stored_key):
    """Re-submitting the exact display form get_all_settings() returned
    ("••••••••tail" or "configured") keeps the stored key."""
    config_service.update_setting("codex_integration", "api_key", stored_key)
    shown = config_service.get_all_settings()["codex_integration"]["api_key"]

    config_service.update_setting("codex_integration", "api_key", shown)

    assert _stored_codex_key(config_service) == stored_key


@pytest.mark.parametrize("blank", ["", None])
def test_blank_api_key_keeps_stored_key(config_service, blank):
    """The key is write-only: a blank value means "keep" (as for A6's
    Langfuse secret_key and OIDC client_secret)."""
    config_service.update_setting("codex_integration", "api_key", "dummy-real-key")

    config_service.update_setting("codex_integration", "api_key", blank)

    assert _stored_codex_key(config_service) == "dummy-real-key"


def test_section_save_with_blank_api_key_keeps_stored_key(config_service):
    """The Codex form posts every field, api_key blank (its input is never
    pre-filled): saving only a new codex_weight must not wipe the key."""
    config_service.update_setting("codex_integration", "api_key", "dummy-real-key")

    config_service.update_settings_audited(
        [
            ("codex_integration", "enabled", "true"),
            ("codex_integration", "credential_mode", "api_key"),
            ("codex_integration", "api_key", ""),
            ("codex_integration", "lcp_url", ""),
            ("codex_integration", "lcp_vendor", "openai"),
            ("codex_integration", "codex_weight", "0.7"),
        ],
        actor="example-admin",
    )

    assert _stored_codex_key(config_service) == "dummy-real-key"
    codex = config_service.get_config().codex_integration_config
    assert codex.codex_weight == pytest.approx(0.7)


@pytest.mark.parametrize(
    "new_key",
    [_MASKED_PLACEHOLDER, "•" * 8 + "abcd-and-more-characters"],
    ids=["contains-stars", "starts-with-mask-chars"],
)
def test_key_merely_containing_mask_characters_is_stored(config_service, new_key):
    """Only the EXACT display forms are placeholders; any other non-blank
    value is a new key and is stored."""
    config_service.update_setting("codex_integration", "api_key", "dummy-real-key")

    config_service.update_setting("codex_integration", "api_key", new_key)

    assert _stored_codex_key(config_service) == new_key


# ---------------------------------------------------------------------------
# 6. Unknown key raises ValueError
# ---------------------------------------------------------------------------


def test_unknown_key_raises_value_error(config_service):
    """update_setting with unknown key must raise ValueError."""
    with pytest.raises(ValueError, match="Unknown codex_integration setting"):
        config_service.update_setting("codex_integration", "nonexistent_key", "value")
