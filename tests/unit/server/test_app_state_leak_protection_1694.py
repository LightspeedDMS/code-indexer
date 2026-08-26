"""
Bug #1694: durable, tree-wide fix for the app.state singleton leak class
first narrowly fixed (per-file save/restore) by Bug #1675.

Bug #1675 fixed three `tests/unit/server/web/` files whose module-scoped
`client` fixture ran `with TestClient(app) as tc:` against the SHARED
`code_indexer.server.app.app` singleton -- this runs the REAL FastAPI
lifespan, which wires a real `DependencyMapService` (bound to this
machine's actual golden-repos directory) onto `app.state`. Lifespan
shutdown stops the scheduler thread but never resets that app.state
attribute, so the stale real service leaks into any later test file in the
same pytest session that reads `app.state.dependency_map_service`.

Bug #1694 found the IDENTICAL defect shape in four more files under
`tests/unit/server/routers/` (`test_git_cat_endpoint.py`,
`test_git_file_history_endpoint.py`, `test_git_blame_endpoint.py`,
`test_repos_sync_status_endpoint.py`), plus the same `with TestClient(app)`
shape recurring in `tests/unit/server/test_custom_group_*.py`,
`tests/unit/server/middleware/test_correlation_delegates_to_bridge_1632.py`,
and `tests/unit/server/telemetry/test_request_tracing.py` -- confirming
this is a recurring CLASS of leak, not a one-off. Rather than adding a
5th/6th/7th per-file save/restore patch, this fix adds ONE tree-wide
`autouse` fixture in `tests/unit/server/conftest.py`
(`_snapshot_restore_shared_app_state`) that snapshots and restores the
shared `app.state` dict around every test in the directory, closing the
entire class (any `app.state.*` attribute a real lifespan run sets -- not
just `dependency_map_service`) in one place.

IMPORTANT re: what is "the system under test" here, mirroring the
established rationale in
`test_background_job_manager_universal_teardown_1635.py`: the function
under test, `_snapshot_restore_shared_app_state_impl`, IS the
conftest.py fixture-generator itself. Driving it directly via `next()`
(rather than only observing it indirectly through pytest's fixture
machinery) lets these tests assert its exact restore semantics --
removal of an added key, and reversion of a changed key's value --
against the REAL shared `app.state` object, with no test double standing
in for either the generator or `app.state`.

Because the REAL autouse fixture of the same name is ALSO active for
every test in this suite (this file lives under `tests/unit/server/`),
each test here saves and restores `app.state.dependency_map_service`
itself around its own manually-driven generator (restoring absence with
`delattr`, never fabricating a `None` value that did not previously
exist), so it cannot leak into sibling tests via the very mechanism it is
testing.
"""

from __future__ import annotations

from code_indexer.server.app import app as shared_app
from tests.unit.server.conftest import _snapshot_restore_shared_app_state_impl


def test_leaked_app_state_attribute_is_restored_after_teardown() -> None:
    """A NEW attribute bound onto app.state mid-test (mimicking what a real
    lifespan run does, e.g. Bug #1675/#1694's `dependency_map_service`)
    must be REMOVED -- not merely left stale -- once the generator's
    teardown phase runs. Discriminating input: an attribute that did NOT
    exist in the pre-test snapshot at all, so a naive "only restore known
    keys" implementation that never deletes anything would fail this.
    """
    assert not hasattr(shared_app.state, "_leak_probe_1694")

    gen = _snapshot_restore_shared_app_state_impl()
    next(gen)  # setup phase: snapshot taken before any mutation

    try:
        shared_app.state._leak_probe_1694 = "leaked-value"
        assert shared_app.state._leak_probe_1694 == "leaked-value"
    finally:
        try:
            next(gen)  # teardown phase: restore
        except StopIteration:
            pass

    assert not hasattr(shared_app.state, "_leak_probe_1694"), (
        "Bug #1694: an app.state attribute added mid-test was not removed "
        "by the shared app.state snapshot/restore fixture's teardown."
    )


def test_changed_app_state_attribute_value_is_reverted_after_teardown() -> None:
    """A PRE-EXISTING attribute whose value is mutated mid-test must be
    reverted to its original (pre-test) value on teardown. Discriminating
    input: a key that already existed before setup, distinguishing this
    from a naive "only delete newly-added keys" implementation that would
    leave a changed pre-existing value stale.
    """
    had_original_value = hasattr(shared_app.state, "dependency_map_service")
    original_value = getattr(shared_app.state, "dependency_map_service", None)
    try:
        shared_app.state.dependency_map_service = "original-sentinel-1694"

        gen = _snapshot_restore_shared_app_state_impl()
        next(gen)  # setup phase: snapshot taken with the sentinel in place

        shared_app.state.dependency_map_service = "leaked-real-service-1694"
        assert shared_app.state.dependency_map_service == "leaked-real-service-1694"

        try:
            next(gen)  # teardown phase: restore
        except StopIteration:
            pass

        assert shared_app.state.dependency_map_service == "original-sentinel-1694", (
            "Bug #1694: an app.state attribute changed mid-test was not "
            "reverted to its pre-test value by the shared app.state "
            "snapshot/restore fixture's teardown."
        )
    finally:
        if had_original_value:
            shared_app.state.dependency_map_service = original_value
        else:
            delattr(shared_app.state, "dependency_map_service")
