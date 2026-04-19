"""
Tests for fault injection bootstrap config keys in ServerConfig.

Story #746 — Phase E: bootstrap config keys.

Verifies:
  - ServerConfig has fault_injection_enabled and fault_injection_nonprod_ack
    fields with correct defaults (False, False).
  - ServerConfigManager.load_config() reads both fields from JSON.
  - Missing fields in JSON default to False.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from code_indexer.server.utils.config_manager import ServerConfig, ServerConfigManager


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _load_config_from_dict(tmp_path: Path, extra: dict) -> ServerConfig:
    """Write a minimal config.json with *extra* keys and return the loaded config."""
    config_data = {"server_dir": str(tmp_path)}
    config_data.update(extra)
    (tmp_path / "config.json").write_text(json.dumps(config_data))
    mgr = ServerConfigManager(server_dir_path=str(tmp_path))
    cfg = mgr.load_config()
    assert cfg is not None, "load_config() returned None unexpectedly"
    return cfg


# ---------------------------------------------------------------------------
# ServerConfig constructor: parametrized over (kwargs, field, expected_value)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "constructor_kwargs, field_name, expected_value",
    [
        ({}, "fault_injection_enabled", False),
        ({}, "fault_injection_nonprod_ack", False),
        ({"fault_injection_enabled": True}, "fault_injection_enabled", True),
        ({"fault_injection_nonprod_ack": True}, "fault_injection_nonprod_ack", True),
        (
            {"fault_injection_enabled": True, "fault_injection_nonprod_ack": True},
            "fault_injection_enabled",
            True,
        ),
        (
            {"fault_injection_enabled": True, "fault_injection_nonprod_ack": True},
            "fault_injection_nonprod_ack",
            True,
        ),
    ],
    ids=[
        "default-enabled",
        "default-ack",
        "explicit-enabled-true",
        "explicit-ack-true",
        "both-true-enabled-field",
        "both-true-ack-field",
    ],
)
def test_server_config_constructor_fault_injection_field(
    tmp_path: Path,
    constructor_kwargs: dict,
    field_name: str,
    expected_value: bool,
):
    """ServerConfig constructor respects fault injection field defaults and explicit values."""
    cfg = ServerConfig(server_dir=str(tmp_path), **constructor_kwargs)
    assert getattr(cfg, field_name) is expected_value


# ---------------------------------------------------------------------------
# load_config(): parametrized over (json_payload, enabled, ack)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "json_payload, expected_enabled, expected_ack",
    [
        ({}, False, False),
        ({"fault_injection_enabled": True}, True, False),
        ({"fault_injection_nonprod_ack": True}, False, True),
        (
            {"fault_injection_enabled": True, "fault_injection_nonprod_ack": True},
            True,
            True,
        ),
    ],
    ids=["absent-both", "enabled-only", "ack-only", "both-true"],
)
def test_load_config_fault_injection_fields(
    tmp_path: Path,
    json_payload: dict,
    expected_enabled: bool,
    expected_ack: bool,
):
    """load_config() correctly reads fault injection fields from config.json."""
    cfg = _load_config_from_dict(tmp_path, json_payload)
    assert cfg.fault_injection_enabled is expected_enabled
    assert cfg.fault_injection_nonprod_ack is expected_ack
