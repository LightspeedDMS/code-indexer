"""Guarded read/modify/replace for small files holding encrypted secrets
(LLM lease state, the legacy CI token JSON file).

Every reader that may rewrite, and every writer, holds an exclusive
``fcntl.flock`` on a sibling ``<name>.lock`` file, so worker processes and
threads never interleave a read-then-replace with another write. Writes go
to a unique 0600 temporary file in the same directory, are fsynced, then
atomically replace the target, and the directory is fsynced.
"""

from __future__ import annotations

import fcntl
import os
import tempfile
import threading
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

LOCK_WAIT_SECONDS = 10.0
# Bounded lock backoff between non-blocking attempts (total <= wait bound).
_LOCK_RETRY_SECONDS = 0.05


class SecretFileLockTimeout(RuntimeError):
    """The file lock was not obtained within LOCK_WAIT_SECONDS."""


@contextmanager
def file_lock(path: Path, wait_seconds: float = LOCK_WAIT_SECONDS) -> Iterator[None]:
    """Hold an exclusive lock for *path* (bounded wait)."""
    lock_path = path.with_name(path.name + ".lock")
    fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        deadline = time.monotonic() + wait_seconds
        waiter = threading.Event()  # never set: a bounded pause between tries
        for _ in range(int(wait_seconds / _LOCK_RETRY_SECONDS) + 2):
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise SecretFileLockTimeout(f"lock for {path} not obtained")
                waiter.wait(_LOCK_RETRY_SECONDS)
        else:
            raise SecretFileLockTimeout(f"lock for {path} not obtained")
        yield
    finally:
        os.close(fd)  # closing the descriptor releases the lock


def read_bytes_or_none(path: Path) -> Optional[bytes]:
    try:
        return path.read_bytes()
    except FileNotFoundError:
        return None


def write_all(fd: int, data: bytes) -> None:
    """Write every byte of *data*; a write that makes no progress raises."""
    view = memoryview(data)
    for _ in range(len(data) + 1):
        if not view:
            return
        written = os.write(fd, view)
        if written <= 0:
            raise OSError("write made no progress")
        view = view[written:]
    raise OSError("short write")


def fsync_dir(directory: Path) -> None:
    dir_fd = os.open(directory, os.O_RDONLY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def write_atomically(path: Path, data: str) -> None:
    """Replace *path* with *data* (unique 0600 temp file, fsync, replace)."""
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        try:
            write_all(fd, data.encode("utf-8"))
            os.fsync(fd)
        finally:
            os.close(fd)
        os.replace(tmp, path)
    except BaseException:
        if os.path.lexists(tmp):
            os.unlink(tmp)
        raise
    fsync_dir(path.parent)
