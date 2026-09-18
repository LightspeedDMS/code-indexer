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


class _FakeStat:
    """Minimal stand-in for os.stat_result exposing only the three
    attributes write_json_atomic reads (st_mode, st_uid, st_gid)."""

    def __init__(self, mode: int, uid: int, gid: int) -> None:
        self.st_mode = mode
        self.st_uid = uid
        self.st_gid = gid


def test_write_json_atomic_preserves_ownership_of_existing_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Bug #1896 (P1, clustered staging): mkstemp+os.replace creates a NEW
    inode owned by the writer. Preserving MODE alone is not enough when the
    writer runs as a different user than the file's real owner (e.g. the
    root auto-updater rewriting a code-indexer server's bootstrap
    config.json) -- with the preserved 0600 mode plus writer (root)
    ownership, the real owner is locked out of its own config and the
    server crash-loops with PermissionError. write_json_atomic() must
    restore the ORIGINAL owner/group on the TEMP FILE before os.replace.

    This is the discriminating RED->GREEN test: it fails before the fix
    (no os.chown call is ever made) and passes after."""
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"marker": "initial"}))
    real_mode = os.stat(config_path).st_mode
    foreign_uid = 424242
    foreign_gid = 434343

    real_stat = os.stat

    def fake_stat(path: object, *args: object, **kwargs: object) -> object:
        if str(path) == str(config_path):
            return _FakeStat(real_mode, foreign_uid, foreign_gid)
        return real_stat(path, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(config_module.os, "stat", fake_stat)

    chown_calls: list = []

    def fake_chown(path: str, uid: int, gid: int) -> None:
        chown_calls.append((path, uid, gid))

    monkeypatch.setattr(config_module.os, "chown", fake_chown)

    write_json_atomic(config_path, {"marker": "updated"})

    assert len(chown_calls) == 1, (
        f"expected exactly one os.chown call restoring the original owner, "
        f"got {chown_calls}"
    )
    called_path, called_uid, called_gid = chown_calls[0]
    assert (called_uid, called_gid) == (foreign_uid, foreign_gid), (
        f"expected os.chown to restore original owner "
        f"({foreign_uid}, {foreign_gid}), got ({called_uid}, {called_gid})"
    )
    # The discriminator from review: chown must target the TEMP file
    # (pre-replace), never the live target path directly.
    assert called_path != str(config_path), (
        "os.chown must be called on the temp file BEFORE os.replace, not "
        "directly on the live target path"
    )
    assert Path(called_path).parent == config_path.parent
    assert Path(called_path).name.endswith(".tmp"), (
        f"expected chown to target the mkstemp(suffix='.tmp') temp file, "
        f"got {called_path!r}"
    )
    assert json.loads(config_path.read_text()) == {"marker": "updated"}


def test_write_json_atomic_degrades_gracefully_when_chown_denied(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A non-root writer replacing a file it already owns cannot chown to a
    different owner and does not need to -- write_json_atomic() must
    degrade gracefully (keep writer ownership, matching pre-#1894 in-place
    open("w") semantics) rather than letting a PermissionError from
    os.chown propagate and fail the whole write."""
    config_path = tmp_path / "config.json"
    config_path.write_text(json.dumps({"marker": "initial"}))
    os.chmod(config_path, 0o644)

    def fake_chown(path: str, uid: int, gid: int) -> None:
        raise PermissionError("Operation not permitted")

    monkeypatch.setattr(config_module.os, "chown", fake_chown)

    write_json_atomic(config_path, {"marker": "updated"})  # must not raise

    assert json.loads(config_path.read_text()) == {"marker": "updated"}
    assert _mode(config_path) == 0o644


def test_write_json_atomic_new_file_does_not_attempt_ownership_restore(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A brand-new file (no preexisting target) has no prior owner to
    preserve -- write_json_atomic() must NOT call os.chown at all in that
    case, leaving the new file writer-owned."""
    config_path = tmp_path / "config.json"
    assert not config_path.exists()

    chown_calls: list = []

    def fake_chown(path: str, uid: int, gid: int) -> None:
        chown_calls.append((path, uid, gid))

    monkeypatch.setattr(config_module.os, "chown", fake_chown)

    write_json_atomic(config_path, {"marker": "fresh"})

    assert chown_calls == [], (
        f"expected no os.chown call for a brand-new file (nothing to "
        f"preserve), got {chown_calls}"
    )
    assert json.loads(config_path.read_text()) == {"marker": "fresh"}
    assert _mode(config_path) == 0o644
