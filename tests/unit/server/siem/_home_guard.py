"""Regression guard: a SIEM test must never open (or connect to) anything
under the REAL ``~/.cidx-server`` -- the home of a developer's running server.

An audit hook (installed once; audit hooks cannot be removed) records every
``open`` / ``sqlite3.connect`` of such a path made on the thread running the
current test.  The home is resolved from the password database, so a
monkeypatched ``HOME`` cannot hide it.  Only the test's own thread is watched:
threads leaked by other suites must not fail a SIEM test.
"""

from __future__ import annotations

import os
import pwd
import sys
import threading
import traceback
from typing import Any, List, Optional

REAL_SERVER_HOME = os.path.realpath(
    os.path.join(pwd.getpwuid(os.getuid()).pw_dir, ".cidx-server")
)
_URI_PREFIX = "file:"
_MAX_FRAMES = 6
_lock = threading.Lock()  # guards _watched and _hits
_watched: List[Optional[int]] = [None]  # thread ident of the running test
_hits: List[str] = []
_tls = threading.local()  # re-entrancy (formatting a stack opens files)


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


def _hook(event: str, args: Any) -> None:
    if event not in ("open", "sqlite3.connect") or not args:
        return
    if threading.get_ident() != _watched[0] or getattr(_tls, "busy", False):
        return
    _tls.busy = True
    try:
        path = _real_home_path(args[0])
        if path is not None:
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
