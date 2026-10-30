from __future__ import annotations

import functools
import sys
import threading
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

import pytest

from tests.utils.real_process_guard import ALLOWING_MARKERS

APP_MODULE = "code_indexer.server.app"
# ^ coupled _SERVER_TESTS with scripts/unit_lane_ignores.txt `tests/unit/server/`; those tests build apps on purpose
_SERVER_TESTS = Path(__file__).resolve().parents[1] / "unit" / "server"
_FIX = (
    "bind the attribute the code path reads with the bind_server_app_attr fixture"
    " (tests/unit/conftest.py), or patch the seam that reads code_indexer.server.app"
)
_GUARD_FLAG = "_server_app_build_guard"

# ^ why plain globals, not contextvars; a lazy read from a worker thread never sees a contextvar
_active: Optional[str] = None
_violations: Dict[str, List[str]] = {}
_lock = threading.Lock()
_built_before_key = pytest.StashKey[bool]()


class ServerAppBuiltInUnitTest(Exception):
    pass


def is_guarded(path: Path, has_allowing_marker: bool) -> bool:
    return not has_allowing_marker and not path.resolve().is_relative_to(_SERVER_TESTS)


def is_guard(create_app: Any) -> bool:
    return getattr(create_app, _GUARD_FLAG, False) is True


def _violation(nodeid: str, how: str) -> str:
    message = (
        f"{nodeid} built the whole server app ({how}) in a fast-lane unit test,"
        f" paying full service bootstrap: {_FIX}"
    )
    with _lock:
        _violations.setdefault(nodeid, []).append(message)
    return message


def guard_create_app(real: Callable[[], Any]) -> Callable[[], Any]:
    @functools.wraps(real)
    def guarded_create_app() -> Any:
        nodeid = _active
        if nodeid is None:
            return real()
        raise ServerAppBuiltInUnitTest(
            _violation(nodeid, "a lazy code_indexer.server.app attribute read")
        )

    setattr(guarded_create_app, _GUARD_FLAG, True)
    return guarded_create_app


def take_violations(nodeid: str) -> List[str]:
    with _lock:
        return _violations.pop(nodeid, [])


def _install_if_loaded() -> None:
    module = sys.modules.get(APP_MODULE)
    if module is not None and not is_guard(module.create_app):
        setattr(module, "create_app", guard_create_app(module.create_app))


def _app_built() -> bool:
    module = sys.modules.get(APP_MODULE)
    return module is not None and module.__dict__.get("_initialized") is True


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_protocol(item: Any, nextitem: Any) -> Any:
    global _active
    _install_if_loaded()
    item.stash[_built_before_key] = _app_built()
    guarded = is_guarded(Path(str(item.path)), _has_allowing_marker(item))
    _active = item.nodeid if guarded else None
    try:
        yield
    finally:
        _active = None


def _fail_report(outcome: Any, nodeid: str) -> None:
    messages = take_violations(nodeid)
    if messages:
        report = outcome.get_result()
        report.outcome = "failed"
        report.longrepr = "\n".join(messages)


def _has_allowing_marker(node: Any) -> bool:
    return any(node.get_closest_marker(m) is not None for m in ALLOWING_MARKERS)


@pytest.hookimpl(hookwrapper=True)
def pytest_make_collect_report(collector: Any) -> Any:
    guarded = isinstance(collector, pytest.Module) and is_guarded(
        Path(str(collector.path)), False
    )
    if not guarded:
        yield
        return
    _install_if_loaded()
    built_before = _app_built()
    outcome = yield
    # ^ why detect after import, not block during it; pytestmark unknown until imported; test:test_guard_allows_marked_module_that_builds_at_import
    if not built_before and _app_built() and not _has_allowing_marker(collector):
        _violation(collector.nodeid, "create_app ran while importing the module")
    _fail_report(outcome, collector.nodeid)


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item: Any, call: Any) -> Any:
    outcome = yield
    # ^ why also an unbuilt-to-built check, not the wrapper alone; server.app first imported in this test => create_app unwrapped
    if (
        call.when == "teardown"
        and _active == item.nodeid
        and item.stash.get(_built_before_key, True) is False
        and _app_built()
    ):
        _violation(item.nodeid, "create_app ran before the guard could wrap it")
    _fail_report(outcome, item.nodeid)
