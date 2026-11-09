"""The local content writer: writes a repository's files on this pod's disk.

Registered on ``code_indexer.content_writer`` as ``local``. Same contract as
the delegate (the content service ``POST /content/write``):

* paths are ABSOLUTE (the content backend's root plus the repo-relative
  path); a relative one is ``EINVAL``;
* nothing under a ``.git`` component is written (``EFORBIDDEN``), and a
  symlink is never written through or deleted (``EFORBIDDEN``);
* ``expected_blob_sha`` is a compare-and-swap on git's blob sha: absent (the
  :data:`~cidx_sdk.seams.content_writer.UNSET` default) is unconditional,
  ``None`` means "must not exist", a sha means "must currently be exactly
  this blob"; a failed precondition is ``ECONFLICT``;
* a write is atomic: the bytes land in a sibling temporary file that replaces
  the target, so a reader sees the old file or the new one, never a torn one;
* any other I/O failure is ``EIO``.

The precondition is checked immediately before the replace, in this process;
it is not a lock. Two writers on different pods racing on one path is the
case the delegated writer (one service owning the files) exists for.
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from typing import Any, Optional

from cidx_sdk.seams.content_identity import git_blob_hash, git_blob_hash_bytes
from cidx_sdk.seams.content_writer import (
    ECONFLICT,
    EFORBIDDEN,
    EINVAL,
    EIO,
    UNSET,
    ContentWriteError,
)

_GIT_DIR = ".git"
_NEW_FILE_MODE = 0o666  # narrowed by the process umask, as open() would


class LocalContentWriter:
    """Writes and deletes files on this pod's disk (see module docstring)."""

    def put(self, path: str, data: bytes, *, expected_blob_sha: Any = UNSET) -> str:
        target = _target(path)
        current = _current_blob(target, path)
        _require(expected_blob_sha, current, path)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            _replace_atomically(target, data)
        except OSError as exc:
            raise ContentWriteError(EIO, f"{path}: {exc.strerror or exc}") from exc
        return git_blob_hash_bytes(data)

    def delete(self, path: str, *, expected_blob_sha: Any = UNSET) -> bool:
        target = _target(path)
        current = _current_blob(target, path)
        _require(expected_blob_sha, current, path)
        if current is None:
            return False
        try:
            target.unlink()
        except FileNotFoundError:
            return False
        except OSError as exc:
            raise ContentWriteError(EIO, f"{path}: {exc.strerror or exc}") from exc
        return True


def _target(path: str) -> Path:
    if not isinstance(path, str) or not os.path.isabs(path):
        raise ContentWriteError(EINVAL, "the writer takes absolute paths")
    target = Path(path)
    if _GIT_DIR in target.parts:
        raise ContentWriteError(EFORBIDDEN, "writes under .git are refused")
    if target.is_symlink():
        raise ContentWriteError(EFORBIDDEN, f"{path} is a symlink")
    return target


def _current_blob(target: Path, path: str) -> Optional[str]:
    """The blob sha of the file at *target*, None when there is none."""
    if not target.exists():
        return None
    if not target.is_file():
        raise ContentWriteError(EINVAL, f"{path} is not a regular file")
    digest = git_blob_hash(target)
    if digest is None:
        raise ContentWriteError(EIO, f"{path} could not be read")
    return digest


def _require(expected: Any, current: Optional[str], path: str) -> None:
    if expected is UNSET or expected == current:
        return
    wanted = "absent" if expected is None else "at the expected blob"
    raise ContentWriteError(ECONFLICT, f"{path} is not {wanted}")


def _replace_atomically(target: Path, data: bytes) -> None:
    mode = target.stat().st_mode & 0o7777 if target.exists() else _NEW_FILE_MODE
    temp = target.with_name(f".{target.name}.cidx-write-{uuid.uuid4().hex}")
    fd = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        if target.exists():
            os.chmod(temp, mode)
        os.replace(temp, target)
    except BaseException:
        temp.unlink(missing_ok=True)
        raise


def make_content_writer() -> LocalContentWriter:
    """Entry point ``local`` on ``code_indexer.content_writer``."""
    return LocalContentWriter()
