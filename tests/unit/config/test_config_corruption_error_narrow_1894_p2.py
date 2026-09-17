"""
Discriminating tests for the P2 review finding on #1894: ConfigManager.load()
previously wrapped EVERY exception (bad JSON, PermissionError, OSError,
pydantic schema-validation rejections) into a bare `ValueError`, and
cli.py's `cidx init --force` self-heal (Bug #1894 RC2) treated ANY such
ValueError as "corrupted" and silently regenerated the config with
defaults -- discarding user settings even for a transient PermissionError
or a recoverable schema-validation failure. Since this self-heal runs
autonomously via RefreshScheduler across ~900 production repos, an
over-broad catch is a fleet-wide data-loss vector (Anti-Fallback /
Anti-Silent-Failure).

The fix introduces `ConfigCorruptionError(ValueError)`, raised ONLY for
genuine corruption (invalid JSON / non-object JSON). Everything else
(PermissionError, OSError, pydantic ValidationError, legacy-field
ValueError) propagates with its NATIVE type. cli.py's --force regenerate
path now catches ONLY ConfigCorruptionError.

These tests fail on the BEHAVIOUR (wrong exception type raised / config
regenerated when it should not be) before the fix, not on a missing
symbol.
"""

import json
import os
from pathlib import Path

import pytest

from code_indexer.config import ConfigCorruptionError, ConfigManager

_RUNNING_AS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0


def test_bad_json_raises_config_corruption_error(tmp_path: Path) -> None:
    """Genuinely corrupt (malformed) JSON must raise the NARROW
    ConfigCorruptionError, not a bare ValueError -- so downstream callers
    (cli.py's --force self-heal) can distinguish real corruption from other
    failure modes."""
    config_path = tmp_path / ".code-indexer" / "config.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text("{not valid json")

    manager = ConfigManager(config_path)

    with pytest.raises(ConfigCorruptionError):
        manager.load()


def test_non_object_json_raises_config_corruption_error(tmp_path: Path) -> None:
    """Valid JSON that isn't an object (e.g. a bare string left by a
    partially-corrupted write) is corruption too -- must also raise
    ConfigCorruptionError, not propagate an unrelated AttributeError from
    treating it like a dict."""
    config_path = tmp_path / ".code-indexer" / "config.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text('""')

    manager = ConfigManager(config_path)

    with pytest.raises(ConfigCorruptionError):
        manager.load()


@pytest.mark.skipif(
    _RUNNING_AS_ROOT, reason="permission bits are not enforced for root"
)
def test_permission_error_propagates_natively_not_as_corruption(
    tmp_path: Path,
) -> None:
    """A transient PermissionError reading config.json must propagate with
    its NATIVE type -- it is not corruption, and must NOT be classified as
    ConfigCorruptionError (which would trigger --force regeneration and
    destroy recoverable user config)."""
    config_path = tmp_path / ".code-indexer" / "config.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(json.dumps({"codebase_dir": str(tmp_path)}))
    os.chmod(config_path, 0o000)

    manager = ConfigManager(config_path)

    try:
        with pytest.raises(PermissionError):
            manager.load()
    finally:
        os.chmod(config_path, 0o644)


def test_legacy_field_validation_error_is_not_config_corruption_error(
    tmp_path: Path,
) -> None:
    """A schema/legacy-field rejection (an otherwise-recoverable config --
    the user just needs to remove one key) raises the native ValueError
    from _validate_no_legacy_config, but must NOT be a ConfigCorruptionError
    -- it is not corrupt JSON, and must not trigger blind --force
    regeneration."""
    config_path = tmp_path / ".code-indexer" / "config.json"
    config_path.parent.mkdir(parents=True)
    config_path.write_text(
        json.dumps(
            {
                "codebase_dir": str(tmp_path),
                "filesystem_config": {"legacy": True},
            }
        )
    )

    manager = ConfigManager(config_path)

    with pytest.raises(ValueError) as excinfo:
        manager.load()

    assert not isinstance(excinfo.value, ConfigCorruptionError), (
        "a legacy-field validation error is recoverable (user data intact) "
        "and must not be classified as corruption"
    )
