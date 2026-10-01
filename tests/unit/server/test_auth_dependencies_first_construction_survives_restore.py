"""
Follow-up to Bug #1727/#1732: `_snapshot_restore_auth_dependencies` (the
tree-wide autouse fixture in tests/unit/server/conftest.py that snapshots
and restores the `server/auth/dependencies` module-global block around
every test) wipes out `code_indexer.server.app`'s ONE-TIME lazy singleton
construction when that construction happens to occur incidentally, for the
first time in the whole pytest process, DURING a test that never asked for
a real app at all.

Root cause (found via bisection of a real server-fast-automation.sh chunk-5
failure -- `mcp/ + telemetry/ + handlers/` run together as one pytest
invocation):

`tests/unit/server/mcp/test_add_golden_repo_handler.py` does
`with patch("code_indexer.server.app.golden_repo_manager") as mock_manager:`.
`golden_repo_manager` is one of `code_indexer.server.app`'s PEP-562 lazy
attributes (Bug #1638). `unittest.mock.patch` must read the ORIGINAL value
before it can patch and later restore it -- that read is a genuine
`getattr(code_indexer.server.app, "golden_repo_manager")`, which (the FIRST
time anything in the process touches an app.py lazy attribute) triggers
`__getattr__` -> `_ensure_initialized()` -> `create_app()` ->
`create_fastapi_app()`. That function's ONLY real assignment of
`dependencies.jwt_manager` / `user_manager` / `oauth_manager` /
`mcp_credential_manager` / `api_key_manager` / `server_config` for the
ENTIRE process happens right there (app_wiring.py:220-228) -- `create_app()`
is guarded to run exactly once (`_initialized`), so this assignment can
never happen again.

But `_snapshot_restore_auth_dependencies`'s SETUP (function-scoped autouse,
runs BEFORE the test body) already ran and snapshotted the PRE-construction
defaults (`None` for every tracked attribute, since nothing had touched
`code_indexer.server.app` yet). When that same test's TEARDOWN runs, the
fixture "faithfully" restores every tracked attribute back to that
pre-construction `None` snapshot -- permanently discarding the real
managers `create_fastapi_app()` just built, for the rest of the pytest
session. Every later test that expects a working real app (e.g.
`tests/unit/server/mcp/test_authenticate_via_authenticated_endpoint.py`,
whose `admin_api_key` fixture asserts
`auth_deps.user_manager is not None`) then fails with
`AssertionError: user_manager not initialized` or a 401
"Authentication required" from the live `/mcp` endpoint, because the
Bearer-auth dependency chain reads the (now permanently `None`) module
global while `/auth/login` still works via a closure-captured reference
unaffected by this leak.

This exactly parallels the ALREADY-DOCUMENTED and ALREADY-FIXED
`_snapshot_restore_shared_app_state` (Bug #1694) "must never itself trigger
construction, and must not restore state a broader-scoped fixture's setup
already baked into its first snapshot" principle -- but for a *narrower*
window: here the incidental construction happens INSIDE the SAME
function-scoped test whose own restore then destroys it.

The fix teaches `_snapshot_restore_auth_dependencies_impl` to detect this
exact transition -- `code_indexer.server.app._initialized` flipping from
`False` to `True` during the generator's active window -- and skip the
restore in that case, letting the freshly (and permanently) constructed
values become the new ambient state for the rest of the session, instead of
reverting them to the stale pre-construction snapshot.

Driving `_snapshot_restore_auth_dependencies_impl` directly via `next()`
(bypassing pytest's fixture machinery) mirrors the established pattern in
`test_auth_dependencies_leak_protection_1727.py`,
`test_app_state_leak_protection_1694.py`, and
`test_background_job_manager_universal_teardown_1635.py`.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

import code_indexer.server.app as _server_app_module
import code_indexer.server.auth.dependencies as _auth_dependencies_module
from tests.unit.server.conftest import (
    _AUTH_DEPENDENCIES_ATTRS,
    _snapshot_restore_auth_dependencies_impl,
)


def _snapshot_real_values() -> Dict[str, Any]:
    return {
        attr: getattr(_auth_dependencies_module, attr)
        for attr in _AUTH_DEPENDENCIES_ATTRS
    }


def _apply_values(values: Dict[str, Any]) -> None:
    for attr, value in values.items():
        setattr(_auth_dependencies_module, attr, value)


def test_incidental_first_construction_during_test_is_not_reverted_by_restore() -> None:
    """Discriminating repro of the real bisected leak: `_initialized` flips
    False -> True DURING the generator's active window (mimicking
    `create_fastapi_app()` running as a side effect of some unrelated
    `unittest.mock.patch` reading another app.py lazy attribute), and the
    freshly-constructed `dependencies.*` values must survive teardown
    instead of being reverted to the stale pre-construction snapshot.

    Discriminating input: the pre-construction snapshot uses `None`
    (the module's genuine uninitialized default, not an arbitrary sentinel)
    while the post-construction values are distinguishable sentinel
    objects -- so a fixture that unconditionally restores to whatever it
    snapshotted at setup (the pre-fix behavior) fails this test exactly the
    way the real bug did: real managers silently reverting to `None`.
    """
    real_values = _snapshot_real_values()
    real_initialized = _server_app_module._initialized
    try:
        # Simulate the pytest-session state immediately before the FIRST
        # ever touch of any code_indexer.server.app lazy attribute: no
        # construction has happened yet, so every tracked dependencies.*
        # attribute is still at its genuine uninitialized default.
        _server_app_module._initialized = False
        _apply_values({attr: None for attr in _AUTH_DEPENDENCIES_ATTRS})

        gen = _snapshot_restore_auth_dependencies_impl()
        next(gen)  # setup phase: snapshots the pre-construction None values

        # Simulate create_fastapi_app() running mid-test as an incidental
        # side effect of some unrelated app.py lazy-attribute access.
        _server_app_module._initialized = True
        constructed_values = {attr: object() for attr in _AUTH_DEPENDENCIES_ATTRS}
        _apply_values(constructed_values)

        with pytest.raises(StopIteration):
            next(gen)  # teardown phase

        for attr in _AUTH_DEPENDENCIES_ATTRS:
            assert (
                getattr(_auth_dependencies_module, attr) is constructed_values[attr]
            ), (
                f"{attr}: incidental first-ever construction of "
                "code_indexer.server.app during this test was wiped by "
                "the auth-dependencies restore -- create_app() only runs "
                "once per process, so this loss is permanent for the rest "
                "of the pytest session."
            )
    finally:
        _server_app_module._initialized = real_initialized
        _apply_values(real_values)


def test_ordinary_mutation_after_construction_is_still_reverted() -> None:
    """Regression guard for the EXISTING #1727/#1732 protection: when
    `_initialized` is ALREADY `True` for the whole duration of the test
    (the normal case -- construction happened in some earlier test), a
    test's own transient mutation of a tracked attribute must still be
    reverted on teardown exactly as before. The fix must not turn off
    restore protection whenever `_initialized` happens to be `True`; it
    must only skip restore when `_initialized` TRANSITIONS during the
    test's own window.
    """
    real_values = _snapshot_real_values()
    real_initialized = _server_app_module._initialized
    try:
        _server_app_module._initialized = True
        pre_test_sentinels = {attr: object() for attr in _AUTH_DEPENDENCIES_ATTRS}
        _apply_values(pre_test_sentinels)

        gen = _snapshot_restore_auth_dependencies_impl()
        next(gen)  # setup phase

        # _initialized stays True throughout -- no construction transition.
        leaked_values = {attr: object() for attr in _AUTH_DEPENDENCIES_ATTRS}
        _apply_values(leaked_values)

        with pytest.raises(StopIteration):
            next(gen)  # teardown phase

        for attr in _AUTH_DEPENDENCIES_ATTRS:
            assert (
                getattr(_auth_dependencies_module, attr) is pre_test_sentinels[attr]
            ), (
                f"{attr}: ordinary per-test mutation (no construction "
                "transition) must still be reverted by teardown -- the "
                "fix must not weaken this existing #1727/#1732 protection."
            )
    finally:
        _server_app_module._initialized = real_initialized
        _apply_values(real_values)
