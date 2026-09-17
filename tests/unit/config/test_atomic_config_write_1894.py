"""Regression tests for Bug #1894 atomic config.json persistence."""

import json
import os
import stat
from pathlib import Path

import pytest

from code_indexer import config as config_module
from code_indexer.config import Config, ConfigManager, write_json_atomic


def test_config_save_keeps_previous_file_when_serialization_is_interrupted(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config_path = tmp_path / ".code-indexer" / "config.json"
    config_path.parent.mkdir()
    previous = {"codebase_dir": str(tmp_path), "marker": "previous"}
    config_path.write_text(json.dumps(previous))
    manager = ConfigManager(config_path)

    def interrupted_dump(data: object, destination: object, **kwargs: object) -> None:
        del data, kwargs
        destination.write('{"marker": "partial"')  # type: ignore[attr-defined]
        raise OSError("simulated interruption")

    monkeypatch.setattr(config_module.json, "dump", interrupted_dump)

    with pytest.raises(OSError, match="simulated interruption"):
        manager.save(Config(codebase_dir=tmp_path))

    assert config_path.read_text() == json.dumps(previous)


def _mode(path: Path) -> int:
    return stat.S_IMODE(os.stat(path).st_mode)


def test_write_json_atomic_preserves_existing_file_mode(tmp_path: Path) -> None:
    """Bug #879 cross-user-read invariant: overwriting an existing
    config.json must PRESERVE its current permission bits (e.g. 0644, so
    other users/processes can still read it) -- not silently narrow it to
    whatever mkstemp() happens to create the temp file with. This must
    fail if the os.chmod(tmp_name, target_mode) mode-preservation logic in
    write_json_atomic() is ever dropped."""
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"marker": "initial"}))
    os.chmod(config_path, 0o644)
    assert _mode(config_path) == 0o644

    write_json_atomic(config_path, {"marker": "updated"})

    assert _mode(config_path) == 0o644, (
        f"expected mode to remain 0644 after overwrite, got {oct(_mode(config_path))}"
    )
    assert json.loads(config_path.read_text()) == {"marker": "updated"}


def test_write_json_atomic_fresh_write_yields_0644_not_mkstemp_default(
    tmp_path: Path,
) -> None:
    """A fresh write (no preexisting file) must yield 0644 -- NOT
    tempfile.mkstemp()'s restrictive default of 0600, which would make a
    newly-created config.json unreadable by other users/processes that
    need to read it (Bug #879)."""
    config_path = tmp_path / "config.json"
    assert not config_path.exists()

    write_json_atomic(config_path, {"marker": "fresh"})

    assert _mode(config_path) == 0o644, (
        f"expected a fresh write to yield mode 0644, got "
        f"{oct(_mode(config_path))} (mkstemp's default 0600 would leak "
        "through if the chmod-to-target_mode step were dropped)"
    )
