"""Regression guard (Bug #1996): a test must never WRITE under the REAL
``~/.cidx-server`` -- the home of a developer's running server.

An audit hook (installed once; audit hooks cannot be removed) records every
write ATTEMPT on such a path made on the thread running the current test:
``open`` with a write mode/flags, ``sqlite3.connect`` (unless a ``mode=ro``
URI), ``os.mkdir``, ``os.rename`` / ``os.replace`` (either side) and
``os.remove``.  Read-only access is not the defect and is not recorded.  The
home is resolved from the password database, so a monkeypatched ``HOME``
cannot hide it.

It is regression proof, not a sandbox: a clean result is limited evidence.
It does NOT see: other threads (threads leaked by other tests must not fail
an unrelated test), child processes, anything before the watch starts
(imports, collection), or access through an already-open file descriptor.
The PRIMARY control is environment isolation: ``isolate_server_home_env``
points the server data-dir variables away from the real home before any
server module is imported -- via ``tests/_isolated_server_home.py``, the
first import of the root ``tests/conftest.py`` -- and ``e2e-automation.sh``
gives every phase its own fresh client server home.

The hook returns after one set-membership check for every other audit
event, and does path work only while a test thread is being watched.

Used by ``tests/unit/server/conftest.py`` (every server unit test) and
``tests/e2e/conftest.py`` (every e2e test process).
"""

from __future__ import annotations

import os
import pwd
import sys
import tempfile
import threading
import traceback
from typing import Any, List, MutableMapping, Optional

REAL_SERVER_HOME = os.path.realpath(
    os.path.join(pwd.getpwuid(os.getuid()).pw_dir, ".cidx-server")
)
_EVENTS = frozenset(
    {"open", "sqlite3.connect", "os.mkdir", "os.rename", "os.replace", "os.remove"}
)
_TWO_PATH_EVENTS = frozenset({"os.rename", "os.replace"})
_WRITE_MODE_CHARS = frozenset("wax+")
_WRITE_FLAGS = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC
_READ_ONLY_URI = "mode=ro"
_URI_PREFIX = "file:"
_MAX_FRAMES = 8
_lock = threading.Lock()  # guards _watched and _hits
_watched: List[Optional[int]] = [None]  # thread ident of the running test
_hits: List[str] = []
_tls = threading.local()  # re-entrancy (formatting a stack opens files)


def is_under_real_home(arg: Any) -> bool:
    """True when *arg* (a path, bytes path or sqlite URI) is in the real home."""
    return _real_home_path(arg) is not None


def _real_home_path(arg: Any) -> Optional[str]:
    try:
        text = os.fsdecode(arg)
    except TypeError:
        # an integer fd (open(fd)) is not a filesystem path: not watched
        return None
    if not text or text.startswith(":memory:"):
        return None
    if text.startswith(_URI_PREFIX):
        text = text[len(_URI_PREFIX) :].split("?", 1)[0]
    full = os.path.realpath(os.path.abspath(text))
    if full == REAL_SERVER_HOME or full.startswith(REAL_SERVER_HOME + os.sep):
        return full
    return None


def _is_write_attempt(event: str, args: Any) -> bool:
    if event == "open":
        # builtin open(): (path, mode_str, flags); os.open(): (path, None, flags)
        mode = args[1] if len(args) > 1 else None
        if isinstance(mode, str):
            return any(c in mode for c in _WRITE_MODE_CHARS)
        flags = args[2] if len(args) > 2 else 0
        return isinstance(flags, int) and bool(flags & _WRITE_FLAGS)
    if event == "sqlite3.connect":  # (database,) -- bytes on CPython 3.9
        try:
            return _READ_ONLY_URI not in os.fsdecode(args[0])
        except TypeError:
            return True
    return True  # mkdir / rename / replace / remove


def _hook(event: str, args: Any) -> None:
    if event not in _EVENTS or not args:
        return
    if threading.get_ident() != _watched[0] or getattr(_tls, "busy", False):
        return
    if not _is_write_attempt(event, args):
        return
    _tls.busy = True
    try:
        candidates = args[:2] if event in _TWO_PATH_EVENTS else args[:1]
        for candidate in candidates:
            path = _real_home_path(candidate)
            if path is None:
                continue
            stack = "".join(traceback.format_stack(limit=_MAX_FRAMES + 1)[:-1])
            with _lock:
                _hits.append(f"{event} {path}\n{stack}")
    finally:
        _tls.busy = False


_installed = [False]  # audit hooks are permanent: install exactly once


def start() -> None:
    """Watch the calling thread (installs the audit hook on first use)."""
    with _lock:
        if not _installed[0]:
            sys.addaudithook(_hook)
            _installed[0] = True
        _hits.clear()
        _watched[0] = threading.get_ident()


def stop() -> List[str]:
    """Stop watching; return what the watched thread touched."""
    with _lock:
        _watched[0] = None
        hits = list(_hits)
        _hits.clear()
    return hits


def failure_message(hits: List[str]) -> str:
    return f"test touched the real {REAL_SERVER_HOME}:\n" + "\n".join(hits)


def session_scratch_root() -> str:
    """Where per-session scratch homes live: the account home's ``.tmp``."""
    return os.path.join(pwd.getpwuid(os.getuid()).pw_dir, ".tmp")


SERVER_HOME_PREFIX = "cidx-test-server-home-"  # per-session scratch homes


SERVER_DIR_VAR = "CIDX_SERVER_DATA_DIR"  # ServerConfigManager & most services
DATA_DIR_VAR = "CIDX_DATA_DIR"  # auto-updater paths (launch/restart files)
UNIT_DIR_VAR = "SYSTEMD_UNIT_DIR"  # live ExecStart read (launch-key gap-fill)


def isolate_server_home_env(environ: MutableMapping[str, str]) -> Optional[str]:
    """Point the server data-dir variables away from the real home.

    A value that already points elsewhere (e.g. the gate's per-chunk dir) is
    kept; an unset value, or one inside the real home, is replaced.
    ``CIDX_DATA_DIR`` follows ``CIDX_SERVER_DATA_DIR``.  An unset
    ``SYSTEMD_UNIT_DIR`` points at an empty location under it, so a first
    boot never back-fills launch keys from the host's real cidx-server unit.
    Returns the scratch directory this call created (the caller removes it),
    else None.
    """
    created: Optional[str] = None
    server_dir = environ.get(SERVER_DIR_VAR, "")
    if not server_dir or is_under_real_home(server_dir):
        scratch_root = session_scratch_root()
        os.makedirs(scratch_root, exist_ok=True)
        created = tempfile.mkdtemp(prefix=SERVER_HOME_PREFIX, dir=scratch_root)
        server_dir = created
        environ[SERVER_DIR_VAR] = server_dir
    data_dir = environ.get(DATA_DIR_VAR, "")
    if not data_dir or is_under_real_home(data_dir):
        environ[DATA_DIR_VAR] = server_dir
    if not environ.get(UNIT_DIR_VAR):
        environ[UNIT_DIR_VAR] = os.path.join(server_dir, "no-systemd-units")
    return created
