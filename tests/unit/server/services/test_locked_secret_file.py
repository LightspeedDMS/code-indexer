"""Stored-secret files are never replaced by a truncated copy, and the file
lock wait is bounded."""

from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

from code_indexer.server.services.locked_secret_file import (
    SecretFileLockTimeout,
    file_lock,
    write_atomically,
)


def test_a_write_that_makes_no_progress_never_replaces_the_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    target = tmp_path / "state.json"
    target.write_text("original")
    monkeypatch.setattr(os, "write", lambda fd, data: 0)
    with pytest.raises(OSError):
        write_atomically(target, "replacement content")
    monkeypatch.undo()
    assert target.read_text() == "original"
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


def test_lock_held_past_the_bound_times_out(tmp_path: Path) -> None:
    target = tmp_path / "state.json"
    held = threading.Event()
    release = threading.Event()

    def holder() -> None:
        with file_lock(target):
            held.set()
            release.wait(timeout=10)

    thread = threading.Thread(target=holder)
    thread.start()
    try:
        assert held.wait(timeout=10)
        with pytest.raises(SecretFileLockTimeout):
            with file_lock(target, wait_seconds=0.2):
                pass
    finally:
        release.set()
        thread.join(timeout=10)
