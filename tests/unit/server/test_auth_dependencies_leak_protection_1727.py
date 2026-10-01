"""
Bug #1732 (Finding 1, follow-up to #1727): dedicated regression coverage for
`_snapshot_restore_auth_dependencies` -- the tree-wide autouse fixture that
snapshots and restores the `server/auth/dependencies` module-global block
(`jwt_manager`, `user_manager`, `oauth_manager`, `mcp_credential_manager`,
`api_key_manager`, and -- since this same #1732's Finding 2 fix wired it up
-- `server_config`) around every test under tests/unit/server/. This file
generically iterates `_AUTH_DEPENDENCIES_ATTRS` rather than hardcoding a
count, so it automatically covers whichever attributes that tuple tracks.

#1727 added this fixture (mirroring the established
`_snapshot_restore_shared_app_state` / Bug #1694 pattern and the #1635
`BackgroundJobManager` universal teardown pattern) to close a real leak:
`create_fastapi_app()` rebinds this whole attribute block atomically
(app_wiring.py:220-227) with no save/restore, so a test that builds its own
`create_app()` instance permanently clobbers the shared module globals for
every later test in the same pytest session. #1727 shipped with zero
dedicated test of its own save/restore mechanism -- see #1732 for the full
writeup (`grep -rln "1727" tests/` returned only the conftest.py fixture
itself). This file follows the EXACT pattern of the two established
precedents: `test_app_state_leak_protection_1694.py` and
`test_background_job_manager_universal_teardown_1635.py`.

IMPORTANT re: what is "the system under test" here, mirroring both
precedents' own rationale: the function under test,
`_snapshot_restore_auth_dependencies_impl`, IS the conftest.py
fixture-generator itself. Driving it directly via `next()` (bypassing
pytest's fixture machinery, per the fixture's own docstring: "extracted so
a unit test can drive it via next() without pytest's fixture machinery")
lets these tests assert its exact restore semantics against the REAL
`code_indexer.server.auth.dependencies` module -- no test double stands in
for either the generator or the module.

Because the REAL autouse fixture of the same name is ALSO active for every
test in this suite (this file lives under tests/unit/server/), each test
here saves and restores the module's real pre-test attribute values itself
around its own manually-driven generator, so it cannot leak into sibling
tests via the very mechanism it is testing.
"""

from __future__ import annotations

from typing import Any, Dict

import pytest

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


def test_all_tracked_attributes_are_reverted_after_teardown() -> None:
    """Mutate every tracked `dependencies` module-global mid-test (mimicking
    the exact leak shape #1727 fixed -- `create_fastapi_app()`'s atomic
    rebind with no save/restore) and confirm every one reverts to its real
    pre-test value once the generator's teardown phase runs.

    Discriminating input: pre-test values are set to distinguishable
    sentinel objects (not the module's normal `None` default), so a
    hypothetically-broken fixture that merely resets every attribute back
    to `None` on teardown -- rather than snapshotting and restoring the
    ACTUAL prior value -- would fail this test just as surely as one whose
    restore step is missing entirely (e.g. a `try: yield` with no
    `finally` block at all).
    """
    real_values = _snapshot_real_values()
    try:
        pre_test_sentinels = {attr: object() for attr in _AUTH_DEPENDENCIES_ATTRS}
        _apply_values(pre_test_sentinels)

        gen = _snapshot_restore_auth_dependencies_impl()
        next(gen)  # setup phase: snapshot taken with sentinels in place

        leaked_values = {attr: object() for attr in _AUTH_DEPENDENCIES_ATTRS}
        _apply_values(leaked_values)
        for attr in _AUTH_DEPENDENCIES_ATTRS:
            assert getattr(_auth_dependencies_module, attr) is leaked_values[attr]

        with pytest.raises(StopIteration):
            next(gen)  # teardown phase: restore, generator then completes

        for attr in _AUTH_DEPENDENCIES_ATTRS:
            assert (
                getattr(_auth_dependencies_module, attr) is pre_test_sentinels[attr]
            ), (
                f"Bug #1732/#1727: {attr} was not reverted to its pre-test "
                "value by the auth-dependencies snapshot/restore fixture's "
                "teardown."
            )
    finally:
        _apply_values(real_values)


def test_teardown_reverts_only_the_mutated_attribute_and_leaves_others_untouched() -> (
    None
):
    """A test that only touches ONE of the tracked attributes (the common
    real-world case -- e.g. a test that only monkeypatches `jwt_manager`
    via `create_app()`) must still have that single attribute correctly
    reverted, while the others remain exactly as they were.

    Discriminating input: mutating a strict subset (rather than all of them
    at once, as the sibling test above does) guards against an
    implementation that only tracks/restores whichever attribute was
    mutated LAST, or that clobbers untouched attributes on restore
    (e.g. a buggy restore that unconditionally resets every tracked
    attribute to `None` regardless of whether it was ever snapshotted or
    touched).
    """
    real_values = _snapshot_real_values()
    try:
        gen = _snapshot_restore_auth_dependencies_impl()
        next(gen)  # setup phase

        original_jwt_manager = _auth_dependencies_module.jwt_manager
        leaked_sentinel = object()
        setattr(_auth_dependencies_module, "jwt_manager", leaked_sentinel)
        assert _auth_dependencies_module.jwt_manager is leaked_sentinel

        untouched_others = {
            attr: getattr(_auth_dependencies_module, attr)
            for attr in _AUTH_DEPENDENCIES_ATTRS
            if attr != "jwt_manager"
        }

        with pytest.raises(StopIteration):
            next(gen)  # teardown phase

        assert _auth_dependencies_module.jwt_manager is original_jwt_manager, (
            "Bug #1732/#1727: jwt_manager was not reverted to its pre-test "
            "value by the auth-dependencies snapshot/restore fixture's "
            "teardown."
        )
        for attr, value in untouched_others.items():
            assert getattr(_auth_dependencies_module, attr) is value, (
                f"Bug #1732/#1727: {attr} was unexpectedly changed by "
                "teardown despite never having been mutated mid-test."
            )
    finally:
        _apply_values(real_values)
