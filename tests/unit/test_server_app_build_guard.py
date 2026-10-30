from __future__ import annotations

import os
import site
import subprocess
import sys
from pathlib import Path

import pytest

from code_indexer.server import app as app_module
from tests.utils import server_app_build_guard as guard

_UNIT = Path(__file__).resolve().parent
_REPO = _UNIT.parents[1]
_INNER_TIMEOUT_SECONDS = 120
_NODEID = "tests/unit/services/test_example.py::test_reads_app"


def test_unit_test_cannot_build_server_app(monkeypatch):
    built = []
    monkeypatch.setattr(guard, "_active", _NODEID)

    guarded = guard.guard_create_app(lambda: built.append(True))

    with pytest.raises(guard.ServerAppBuiltInUnitTest) as raised:
        guarded()
    assert built == []
    violations = guard.take_violations(_NODEID)
    assert violations == [str(raised.value)]
    assert _NODEID in violations[0]
    assert "bind_server_app_attr" in violations[0]


def test_unguarded_caller_builds_normally(monkeypatch):
    sentinel = object()
    monkeypatch.setattr(guard, "_active", None)

    assert guard.guard_create_app(lambda: sentinel)() is sentinel
    assert guard.take_violations(_NODEID) == []


def test_server_tests_and_marked_tests_are_not_guarded():
    assert guard.is_guarded(_UNIT / "services" / "test_x.py", False)
    assert not guard.is_guarded(_UNIT / "server" / "mcp" / "test_x.py", False)
    assert not guard.is_guarded(_UNIT / "services" / "test_x.py", True)


def test_guard_is_installed_on_loaded_app_module():
    assert guard.is_guard(app_module.create_app)


_INNER_CONFTEST = """
from tests.utils import server_app_build_guard


def pytest_configure(config):
    config.pluginmanager.register(server_app_build_guard, "server-app-build-guard")
"""
_BODY_READ = """
from code_indexer.server import app as app_module


def test_body_read():
    app_module.golden_repo_manager
"""
_IMPORT_TIME_BUILD = """
import pytest

import code_indexer.server.app as app_module

app_module.create_app = lambda: object()
from code_indexer.server.app import golden_repo_manager  # noqa: E402

pytestmark = {marks}


def test_after_import():
    pass
"""


def _run_inner_pytest(tmp_path: Path, files: dict, expected_rc: int) -> str:
    (tmp_path / "conftest.py").write_text(_INNER_CONFTEST)
    for name, source in files.items():
        (tmp_path / name).write_text(source)
    home = tmp_path / "home"
    home.mkdir()
    env = {
        **os.environ,
        "HOME": str(home),
        "PYTHONUSERBASE": site.getuserbase(),
        "CIDX_SERVER_DATA_DIR": str(home / ".cidx-server"),
        "PYTHONPATH": os.pathsep.join([str(_REPO), str(_REPO / "src")]),
    }
    done = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "-p", "no:cacheprovider", "-rfE"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=_INNER_TIMEOUT_SECONDS,
    )
    assert done.returncode == expected_rc, done.stdout + done.stderr
    return done.stdout


def test_guard_fails_lazy_read_in_test_body(tmp_path):
    out = _run_inner_pytest(
        tmp_path,
        {
            "test_a_body.py": _BODY_READ,
            "test_b_clean.py": "def test_clean():\n    pass\n",
        },
        1,
    )
    assert "test_a_body.py::test_body_read built the whole server app" in out
    assert "bind_server_app_attr" in out
    assert "1 failed, 1 passed" in out


def test_guard_fails_module_that_builds_at_import(tmp_path):
    source = _IMPORT_TIME_BUILD.format(marks="[]")
    out = _run_inner_pytest(tmp_path, {"test_a_build.py": source}, 2)
    assert "test_a_build.py built the whole server app" in out
    assert "bind_server_app_attr" in out


def test_guard_allows_marked_module_that_builds_at_import(tmp_path):
    source = _IMPORT_TIME_BUILD.format(marks="pytest.mark.slow")
    out = _run_inner_pytest(tmp_path, {"test_a_build.py": source}, 0)
    assert "built the whole server app" not in out
    assert "1 passed" in out
