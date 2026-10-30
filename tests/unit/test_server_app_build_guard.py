from __future__ import annotations

from pathlib import Path

import pytest

from code_indexer.server import app as app_module
from tests.utils import server_app_build_guard as guard

_UNIT = Path(__file__).resolve().parent
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
