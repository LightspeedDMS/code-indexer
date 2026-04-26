"""
Unit tests for Story #844: CodexIntegrationConfig dataclass.

6 Acceptance Criteria:
  AC1: defaults — enabled=False, credential_mode="none", api_key=None,
       lcp_url=None, lcp_vendor="openai", codex_weight=0.5
  AC2: invalid credential_mode raises ValueError
  AC3: codex_weight outside [0.0, 1.0] raises ValueError
  AC4: asdict roundtrip preserves all fields
  AC5: dict coercion in _dict_to_server_config (mirrors Bug #891 pattern)
       including unknown-key filtering for rolling-upgrade safety
  AC6: codex_integration_config is NOT in BOOTSTRAP_KEYS
"""

import os
from dataclasses import asdict, fields

import pytest

# Clearly synthetic test sentinels — NOT real credentials or endpoints.
_DUMMY_API_KEY = "dummy-api-key-not-real"
_DUMMY_LCP_URL = "https://example.invalid/lcp"


@pytest.fixture
def config_manager(tmp_path):
    from code_indexer.server.utils.config_manager import ServerConfigManager

    server_dir = str(tmp_path / "cidx-server")
    os.makedirs(server_dir, exist_ok=True)
    return ServerConfigManager(server_dir)


# ---------------------------------------------------------------------------
# AC1: Default values (parametrized)
# ---------------------------------------------------------------------------

_DEFAULTS = [
    ("enabled", False),
    ("credential_mode", "none"),
    ("api_key", None),
    ("lcp_url", None),
    ("lcp_vendor", "openai"),
    ("codex_weight", 0.5),
]


@pytest.mark.parametrize(
    "field_name,expected", _DEFAULTS, ids=[d[0] for d in _DEFAULTS]
)
def test_codex_integration_config_defaults(field_name, expected):
    """AC1: CodexIntegrationConfig() must expose correct field defaults."""
    from code_indexer.server.utils.config_manager import CodexIntegrationConfig

    cfg = CodexIntegrationConfig()
    actual = getattr(cfg, field_name)
    if isinstance(expected, float):
        assert actual == pytest.approx(expected)
    else:
        assert actual == expected


# ---------------------------------------------------------------------------
# AC2: credential_mode validation
# ---------------------------------------------------------------------------

_VALID_MODES = ["none", "api_key", "subscription"]


@pytest.mark.parametrize("mode", _VALID_MODES)
def test_valid_credential_modes_accepted(mode):
    """AC2: Valid credential_mode values must be accepted without error."""
    from code_indexer.server.utils.config_manager import CodexIntegrationConfig

    cfg = CodexIntegrationConfig(credential_mode=mode)
    assert cfg.credential_mode == mode


def test_invalid_credential_mode_raises_value_error():
    """AC2: Invalid credential_mode must raise ValueError mentioning 'credential_mode'."""
    from code_indexer.server.utils.config_manager import CodexIntegrationConfig

    with pytest.raises(ValueError, match="credential_mode"):
        CodexIntegrationConfig(credential_mode="invalid")


# ---------------------------------------------------------------------------
# AC3: codex_weight validation
# ---------------------------------------------------------------------------

_INVALID_WEIGHTS = [1.5, -0.1, 2.0]


@pytest.mark.parametrize("weight", _INVALID_WEIGHTS)
def test_codex_weight_out_of_range_raises_value_error(weight):
    """AC3: codex_weight outside [0.0, 1.0] must raise ValueError."""
    from code_indexer.server.utils.config_manager import CodexIntegrationConfig

    with pytest.raises(ValueError, match="codex_weight"):
        CodexIntegrationConfig(codex_weight=weight)


@pytest.mark.parametrize("weight", [0.0, 0.5, 1.0])
def test_valid_codex_weights_accepted(weight):
    """AC3: Boundary and midpoint weights must be accepted."""
    from code_indexer.server.utils.config_manager import CodexIntegrationConfig

    cfg = CodexIntegrationConfig(codex_weight=weight)
    assert cfg.codex_weight == pytest.approx(weight)


# ---------------------------------------------------------------------------
# AC4: asdict serialization roundtrip
# ---------------------------------------------------------------------------


def test_asdict_roundtrip_preserves_all_fields():
    """AC4: asdict + reconstruct from dict must preserve all six field values."""
    from code_indexer.server.utils.config_manager import CodexIntegrationConfig

    original = CodexIntegrationConfig(
        enabled=True,
        credential_mode="api_key",
        api_key=_DUMMY_API_KEY,
        lcp_url=_DUMMY_LCP_URL,
        lcp_vendor="openai",
        codex_weight=0.7,
    )
    d = asdict(original)
    reconstructed = CodexIntegrationConfig(**d)

    field_names = [f.name for f in fields(CodexIntegrationConfig)]
    for name in field_names:
        orig_val = getattr(original, name)
        reco_val = getattr(reconstructed, name)
        if isinstance(orig_val, float):
            assert reco_val == pytest.approx(orig_val), f"field {name} mismatch"
        else:
            assert reco_val == orig_val, f"field {name} mismatch"


# ---------------------------------------------------------------------------
# AC5: Dict coercion in _dict_to_server_config
# ---------------------------------------------------------------------------


def test_dict_to_server_config_coerces_codex_config_dict_and_preserves_values(
    config_manager, tmp_path
):
    """AC5: _dict_to_server_config must coerce codex_integration_config dict to
    dataclass and preserve all six field values."""
    from code_indexer.server.utils.config_manager import CodexIntegrationConfig

    raw = {
        "server_dir": str(tmp_path / "cidx-server"),
        "codex_integration_config": {
            "enabled": True,
            "credential_mode": "api_key",
            "api_key": _DUMMY_API_KEY,
            "lcp_url": None,
            "lcp_vendor": "openai",
            "codex_weight": 0.3,
        },
    }
    config = config_manager._dict_to_server_config(raw)
    cx = config.codex_integration_config
    assert isinstance(cx, CodexIntegrationConfig), (
        f"Expected CodexIntegrationConfig, got {type(cx)}"
    )
    assert cx.enabled is True
    assert cx.credential_mode == "api_key"
    assert cx.api_key == _DUMMY_API_KEY
    assert cx.lcp_url is None
    assert cx.lcp_vendor == "openai"
    assert cx.codex_weight == pytest.approx(0.3)


def test_dict_coercion_filters_unknown_keys(config_manager, tmp_path):
    """AC5: Unknown keys in stored blob are silently dropped (rolling-upgrade safety).

    Asserts that:
    1. The result is a CodexIntegrationConfig instance.
    2. The unknown field is absent from the coerced dataclass.
    3. All six known field values survive filtering unchanged.
    """
    from code_indexer.server.utils.config_manager import CodexIntegrationConfig

    raw = {
        "server_dir": str(tmp_path / "cidx-server"),
        "codex_integration_config": {
            "enabled": False,
            "credential_mode": "none",
            "api_key": None,
            "lcp_url": None,
            "lcp_vendor": "openai",
            "codex_weight": 0.5,
            "future_unknown_field": "ignored",
        },
    }
    config = config_manager._dict_to_server_config(raw)
    cx = config.codex_integration_config
    assert isinstance(cx, CodexIntegrationConfig)
    # Unknown field must NOT be present on the dataclass instance
    assert not hasattr(cx, "future_unknown_field"), (
        "future_unknown_field leaked through — rolling-upgrade filter is broken"
    )
    # All six known field values must survive the filtering
    assert cx.enabled is False
    assert cx.credential_mode == "none"
    assert cx.api_key is None
    assert cx.lcp_url is None
    assert cx.lcp_vendor == "openai"
    assert cx.codex_weight == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# AC6: Not in BOOTSTRAP_KEYS
# ---------------------------------------------------------------------------


def test_codex_integration_config_not_in_bootstrap_keys():
    """AC6: codex_integration_config must NOT be in BOOTSTRAP_KEYS (runtime-DB only)."""
    from code_indexer.server.services.config_service import BOOTSTRAP_KEYS

    assert "codex_integration_config" not in BOOTSTRAP_KEYS


def test_server_config_has_codex_integration_config_field():
    """AC6: ServerConfig must have a codex_integration_config field."""
    from code_indexer.server.utils.config_manager import ServerConfig

    all_fields = {f.name for f in fields(ServerConfig)}
    assert "codex_integration_config" in all_fields
