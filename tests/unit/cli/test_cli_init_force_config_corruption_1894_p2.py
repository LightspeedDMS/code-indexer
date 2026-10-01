"""
Discriminating CLI-level tests for the P2 review finding on #1894:
`cidx init --force`'s self-heal (cli.py ~2333-2348, Bug #1894 RC2) caught a
bare `ValueError` around `ConfigManager.load()` and treated ANY such error
as "corrupted", silently regenerating the config with defaults. Since
ConfigManager.load() used to wrap EVERY exception (including
PermissionError/OSError and pydantic schema-validation failures) into a
plain ValueError, a transient permission problem could destroy a
recoverable config with real user settings. This self-heal runs
autonomously via RefreshScheduler across ~900 production repos
(Anti-Fallback / Anti-Silent-Failure).

After the fix, cli.py catches ONLY `ConfigCorruptionError` (the narrow
subclass ConfigManager.load() now raises for genuine JSON corruption).
Other exception types propagate natively and are never silently converted
into a "regenerate" decision.
"""

import json
import os
from pathlib import Path

import pytest
from click.testing import CliRunner

from code_indexer.cli import cli

_RUNNING_AS_ROOT = hasattr(os, "geteuid") and os.geteuid() == 0


def test_init_force_still_regenerates_genuinely_corrupt_config(
    tmp_path: Path,
) -> None:
    """Non-regression: real JSON corruption must still be repaired under
    --force (this is the Bug #1894 RC2 self-heal's whole purpose)."""
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    cidx_dir = project_dir / ".code-indexer"
    cidx_dir.mkdir()
    config_path = cidx_dir / "config.json"
    config_path.write_text("{not valid json")

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["init", str(project_dir), "--force", "--no-override-file"],
    )

    assert result.exit_code == 0, result.output
    regenerated = json.loads(config_path.read_text())
    assert isinstance(regenerated, dict)


@pytest.mark.skipif(
    _RUNNING_AS_ROOT, reason="permission bits are not enforced for root"
)
def test_init_force_does_not_regenerate_on_permission_error(
    tmp_path: Path,
) -> None:
    """A transient PermissionError reading config.json must NOT be treated
    as corruption -- the original (recoverable) user config must survive
    untouched, and the command must not report "invalid or corrupted"."""
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    cidx_dir = project_dir / ".code-indexer"
    cidx_dir.mkdir()
    config_path = cidx_dir / "config.json"
    original_content = json.dumps(
        {"codebase_dir": str(project_dir), "marker": "user-data"}
    )
    config_path.write_text(original_content)
    os.chmod(config_path, 0o000)

    runner = CliRunner()
    try:
        result = runner.invoke(
            cli,
            ["init", str(project_dir), "--force", "--no-override-file"],
        )
    finally:
        os.chmod(config_path, 0o644)

    assert "is invalid or corrupted" not in result.output, (
        "a PermissionError must not be classified as config corruption"
    )
    assert config_path.read_text() == original_content, (
        "a PermissionError must not trigger --force regeneration -- "
        "user config must survive untouched"
    )


def test_init_force_does_not_regenerate_on_legacy_field_validation_error(
    tmp_path: Path,
) -> None:
    """The true discriminator for cli.py's `except ValueError` ->
    `except ConfigCorruptionError` edit: a legacy-field schema rejection
    (_validate_no_legacy_config) raises a plain ValueError -- which WAS
    (and, pre-fix, still is) caught by a bare `except ValueError` in
    cli.py and silently force-regenerated, discarding the user's config.
    After the fix, only ConfigCorruptionError triggers regeneration, so
    this native ValueError must propagate instead and the original config
    must survive untouched."""
    project_dir = tmp_path / "proj"
    project_dir.mkdir()
    cidx_dir = project_dir / ".code-indexer"
    cidx_dir.mkdir()
    config_path = cidx_dir / "config.json"
    original_content = json.dumps(
        {
            "codebase_dir": str(project_dir),
            "filesystem_config": {"legacy": True},
        }
    )
    config_path.write_text(original_content)

    runner = CliRunner()
    result = runner.invoke(
        cli,
        ["init", str(project_dir), "--force", "--no-override-file"],
    )

    assert "is invalid or corrupted" not in result.output, (
        "a legacy-field validation error is recoverable, not corruption, "
        "and must not trigger blind --force regeneration"
    )
    assert config_path.read_text() == original_content, (
        "a legacy-field validation error must not trigger --force "
        "regeneration -- user config must survive untouched"
    )
