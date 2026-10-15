"""Stored secrets use a random key kept in the server directory (solo) --
never a value derived from host metadata."""

from __future__ import annotations

import base64
import logging
import os
import stat
import threading
from pathlib import Path
from typing import List
from unittest import mock

import pytest

from code_indexer.server.services.stored_secret_key import (
    KEY_FILENAME,
    StoredSecretKeyError,
    load_or_create_key_file,
)


def _uname_with(nodename: str) -> os.uname_result:
    real = os.uname()
    return os.uname_result(
        (real.sysname, nodename, real.release, real.version, real.machine)
    )


def test_key_file_created_with_32_random_bytes_and_mode_0600(tmp_path: Path) -> None:
    key = load_or_create_key_file(tmp_path)
    path = tmp_path / KEY_FILENAME
    assert path.is_file()
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert len(base64.urlsafe_b64decode(key)) == 32


def test_two_server_dirs_on_one_host_get_different_keys(tmp_path: Path) -> None:
    a = load_or_create_key_file(tmp_path / "a")
    b = load_or_create_key_file(tmp_path / "b")
    assert a != b


def test_key_does_not_depend_on_hostname(tmp_path: Path) -> None:
    with mock.patch("os.uname", return_value=_uname_with("host-one.example.com")):
        first = load_or_create_key_file(tmp_path)
    with mock.patch("os.uname", return_value=_uname_with("host-two.example.com")):
        second = load_or_create_key_file(tmp_path)
    assert first == second


def test_existing_key_file_is_never_overwritten(tmp_path: Path) -> None:
    first = load_or_create_key_file(tmp_path)
    before = (tmp_path / KEY_FILENAME).read_bytes()
    again = load_or_create_key_file(tmp_path)
    assert again == first
    assert (tmp_path / KEY_FILENAME).read_bytes() == before


def test_concurrent_creators_converge_on_one_key(tmp_path: Path) -> None:
    results: List[bytes] = []
    errors: List[BaseException] = []
    barrier = threading.Barrier(8)

    def create() -> None:
        try:
            barrier.wait()
            results.append(load_or_create_key_file(tmp_path))
        except BaseException as exc:  # collected and asserted below
            errors.append(exc)

    threads = [threading.Thread(target=create) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert errors == []
    assert len(results) == 8
    assert len(set(results)) == 1
    assert (tmp_path / KEY_FILENAME).read_bytes().strip() == results[0]
    leftovers = [p.name for p in tmp_path.iterdir() if p.name != KEY_FILENAME]
    assert leftovers == []


def test_corrupt_key_file_fails_loudly_and_is_kept(tmp_path: Path) -> None:
    path = tmp_path / KEY_FILENAME
    path.write_bytes(b"not-a-key")
    with pytest.raises(StoredSecretKeyError):
        load_or_create_key_file(tmp_path)
    assert path.read_bytes() == b"not-a-key"


def test_empty_key_file_fails_loudly(tmp_path: Path) -> None:
    (tmp_path / KEY_FILENAME).write_bytes(b"")
    with pytest.raises(StoredSecretKeyError):
        load_or_create_key_file(tmp_path)


def test_symlinked_key_file_fails_loudly(tmp_path: Path) -> None:
    real = tmp_path / "elsewhere"
    real.mkdir()
    target_key = load_or_create_key_file(real)
    (tmp_path / "server").mkdir()
    (tmp_path / "server" / KEY_FILENAME).symlink_to(real / KEY_FILENAME)
    with pytest.raises(StoredSecretKeyError):
        load_or_create_key_file(tmp_path / "server")
    assert load_or_create_key_file(real) == target_key


def test_key_file_with_wider_mode_is_tightened_with_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    key = load_or_create_key_file(tmp_path)
    path = tmp_path / KEY_FILENAME
    os.chmod(path, 0o644)
    caplog.set_level(logging.WARNING)
    assert load_or_create_key_file(tmp_path) == key
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert len([r for r in caplog.records if r.levelno == logging.WARNING]) == 1


def test_key_file_owned_by_another_user_fails_loudly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    load_or_create_key_file(tmp_path)
    other_uid = os.getuid() + 1
    monkeypatch.setattr(os, "getuid", lambda: other_uid)
    with pytest.raises(StoredSecretKeyError):
        load_or_create_key_file(tmp_path)


def test_unreadable_key_file_fails_loudly(tmp_path: Path) -> None:
    path = tmp_path / KEY_FILENAME
    path.mkdir()  # a directory where the key file belongs cannot be read
    with pytest.raises(StoredSecretKeyError):
        load_or_create_key_file(tmp_path)
