"""Bug #1996: the real-server-home guard itself.

Watch scenarios run in a FRESH interpreter: the guard's audit hook is
process-wide, and calling start()/stop() here would replace the autouse watch
that tests/unit/server/conftest.py keeps on this very test.

Every probe is a write ATTEMPT that cannot succeed (missing parent directory,
nonexistent source), so the real home is never modified: the audit event
fires before the operation fails.
"""

from __future__ import annotations

import json
import os
import site
import subprocess
import sys
from pathlib import Path

import pytest

from tests.fixtures import real_server_home_guard as guard

REPO_ROOT = Path(__file__).resolve().parents[3]
SUBPROCESS_TIMEOUT_SECONDS = 60
_MISSING = os.path.join(guard.REAL_SERVER_HOME, "__bug_1996_guard_probe_missing_dir__")

_PRELUDE = f"""
import json, os, sqlite3, threading
from tests.fixtures import real_server_home_guard as guard
MISSING = {_MISSING!r}
assert not os.path.exists(MISSING)

def attempt(fn, *args, **kwargs):
    try:
        fn(*args, **kwargs)
    except (OSError, sqlite3.Error):
        pass
"""


def _watch(body: str) -> list:
    """Run *body* between guard.start()/stop() in a fresh interpreter."""
    code = (
        _PRELUDE
        + "guard.start()\n"
        + body
        + "\nprint(json.dumps([h.splitlines()[0] for h in guard.stop()]))\n"
    )
    env = dict(os.environ)
    env["PYTHONPATH"] = os.pathsep.join(
        [str(REPO_ROOT), str(REPO_ROOT / "src"), site.getusersitepackages()]
    )
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        timeout=SUBPROCESS_TIMEOUT_SECONDS,
        cwd=str(REPO_ROOT),
        env=env,
    )
    assert result.returncode == 0, result.stderr
    assert not os.path.exists(_MISSING), "a probe modified the real home"
    hits: list = json.loads(result.stdout.strip().splitlines()[-1])
    return hits


def test_write_attempts_on_the_watched_thread_are_recorded() -> None:
    hits = _watch(
        "attempt(open, os.path.join(MISSING, 'f.txt'), 'w')\n"
        "attempt(os.open, os.path.join(MISSING, 'g'), os.O_WRONLY | os.O_CREAT)\n"
        "attempt(os.remove, MISSING)\n"
        "attempt(os.mkdir, os.path.join(MISSING, 'sub'))\n"
        "attempt(os.rename, '/nonexistent-bug-1996-src', os.path.join(MISSING, 'dst'))\n"
        "attempt(sqlite3.connect, os.path.join(MISSING, 'x.db'))\n"
    )
    assert hits == [
        f"open {_MISSING}/f.txt",
        f"open {_MISSING}/g",
        f"os.remove {_MISSING}",
        f"os.mkdir {_MISSING}/sub",
        f"os.rename {_MISSING}/dst",
        f"sqlite3.connect {_MISSING}/x.db",
    ]


def test_read_only_access_is_not_recorded() -> None:
    hits = _watch(
        "attempt(open, os.path.join(MISSING, 'f.txt'))\n"
        "attempt(open, os.path.join(MISSING, 'f.txt'), 'rb')\n"
        "attempt(sqlite3.connect, 'file:' + MISSING + '/x.db?mode=ro', uri=True)\n"
    )
    assert hits == []


def test_temp_paths_are_not_recorded(tmp_path: Path) -> None:
    hits = _watch(
        f"base = {str(tmp_path)!r}\n"
        "open(os.path.join(base, 'x.txt'), 'w').close()\n"
        "os.mkdir(os.path.join(base, 'sub'))\n"
        "os.replace(os.path.join(base, 'x.txt'), os.path.join(base, 'sub', 'y'))\n"
    )
    assert hits == []


def test_other_threads_are_not_watched() -> None:
    hits = _watch(
        "t = threading.Thread(target=attempt, args=(os.remove, MISSING))\n"
        "t.start(); t.join()\n"
    )
    assert hits == []


def test_nothing_is_recorded_after_stop() -> None:
    hits = _watch("guard.stop()\nattempt(os.remove, MISSING)\n")
    assert hits == []


def test_isolate_sets_both_vars_to_a_fresh_scratch_dir_when_unset() -> None:
    environ: dict = {}
    created = guard.isolate_server_home_env(environ)
    try:
        assert created is not None and os.path.isdir(created)
        assert not guard.is_under_real_home(created)
        assert environ == {
            "CIDX_SERVER_DATA_DIR": created,
            "CIDX_DATA_DIR": created,
            "SYSTEMD_UNIT_DIR": os.path.join(created, "no-systemd-units"),
        }
    finally:
        if created:
            os.rmdir(created)


def test_isolate_replaces_values_inside_the_real_home() -> None:
    environ = {
        "CIDX_SERVER_DATA_DIR": guard.REAL_SERVER_HOME,
        "CIDX_DATA_DIR": os.path.join(guard.REAL_SERVER_HOME, "data"),
        "SYSTEMD_UNIT_DIR": "/explicit/units",
    }
    created = guard.isolate_server_home_env(environ)
    try:
        assert created is not None
        assert environ == {
            "CIDX_SERVER_DATA_DIR": created,
            "CIDX_DATA_DIR": created,
            "SYSTEMD_UNIT_DIR": "/explicit/units",
        }
    finally:
        if created:
            os.rmdir(created)


def test_isolate_keeps_an_external_dir_and_data_dir_follows(tmp_path: Path) -> None:
    environ = {"CIDX_SERVER_DATA_DIR": str(tmp_path)}
    assert guard.isolate_server_home_env(environ) is None
    assert environ == {
        "CIDX_SERVER_DATA_DIR": str(tmp_path),
        "CIDX_DATA_DIR": str(tmp_path),
        "SYSTEMD_UNIT_DIR": str(tmp_path / "no-systemd-units"),
    }


@pytest.mark.parametrize(
    "arg, expected",
    [
        (guard.REAL_SERVER_HOME, True),
        (os.path.join(guard.REAL_SERVER_HOME, "groups.db"), True),
        ("file:" + os.path.join(guard.REAL_SERVER_HOME, "a.db") + "?mode=ro", True),
        (os.fsencode(os.path.join(guard.REAL_SERVER_HOME, "b.db")), True),
        (guard.REAL_SERVER_HOME + "-sibling", False),
        (":memory:", False),
        (3, False),
        ("", False),
    ],
)
def test_is_under_real_home(arg: object, expected: bool) -> None:
    assert guard.is_under_real_home(arg) is expected
