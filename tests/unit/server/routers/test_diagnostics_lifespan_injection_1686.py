"""Bug #1686 regression: lifespan's Bug #532 diagnostics backend injection
must survive the new lazy `diagnostics_service` singleton.

Bug #1686's fix to routers/diagnostics.py replaced the eager
`diagnostics_service = DiagnosticsService()` module-level statement with a
real `diagnostics_service: Optional[DiagnosticsService] = None` sentinel
plus a `_get_diagnostics_service()` getter, so that a bare import of the
router (directly, or transitively via inline_routes.py) no longer
constructs a real DiagnosticsService and touches the live on-disk DB.

That fix missed a real consumer: `startup/lifespan.py`'s Bug #532 code does

    from code_indexer.server.routers.diagnostics import (
        diagnostics_service as _diagnostics_service,
    )
    _diagnostics_service._backend = backend_registry.diagnostics

which is a bare-name import of the module global -- after Bug #1686's fix
this binds to `None`, and `_backend = ...` raises
`AttributeError: 'NoneType' object has no attribute '_backend'`. This
statement lives directly inside `make_lifespan`'s `async def lifespan`, with
NO enclosing `try`/`except` (confirmed via AST inspection), so the
AttributeError propagates out of the lifespan context manager and aborts
server startup entirely. The injection guard
(`backend_registry is not None and hasattr(backend_registry, "diagnostics")`)
is unconditionally true on a real server (both solo/SQLite and
cluster/PostgreSQL branches of `service_init.py` populate
`BackendRegistry.diagnostics`), so this is not an edge case -- it crashes
every real server boot.

The fix routes this injection through `_get_diagnostics_service()` (a
function call) instead of importing the bare module global, so the
singleton is lazily constructed at real startup time if it hasn't been
already -- construction at server STARTUP (not import time) is exactly what
Bug #1686 always intended to allow.

This test boots the REAL app lifespan (mirroring the established
test_git_cat_endpoint.py pattern: `from code_indexer.server.app import app`
+ `with TestClient(app) as client:`) with the diagnostics singleton
explicitly reset to None beforehand and restored afterward, so it
genuinely discriminates the crash regardless of inter-test pollution from
any other test file's mock.patch calls (the whole reason the reviewer's
original "758 passed" evidence was a false green: test_diagnostics_*.py
sorts alphabetically before test_git_*.py and happened to leave a real,
non-None DiagnosticsService instance sitting in the module global from
prior mock.patch use, masking the crash for every later test in that same
process).
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from code_indexer.server.app import app
from code_indexer.server.routers import diagnostics as diagnostics_router_module


@pytest.fixture()
def diagnostics_singleton_reset_to_none():
    """Explicitly save the current `diagnostics_service` module attribute,
    set it to None for the duration of the test (simulating the real
    server-startup ordering where lifespan's Bug #532 injection runs
    against an as-yet-unconstructed singleton), and restore the original
    value afterward -- regardless of what earlier tests in the same pytest
    process may have already constructed or patched.
    """
    original_value = diagnostics_router_module.diagnostics_service
    diagnostics_router_module.diagnostics_service = None
    try:
        yield
    finally:
        diagnostics_router_module.diagnostics_service = original_value


class TestLifespanDiagnosticsBackendInjectionSurvivesLazySingleton:
    """Bug #1686 regression: real server startup must not crash on the
    lazy diagnostics_service singleton."""

    def test_real_lifespan_boot_does_not_crash_on_none_singleton(
        self, diagnostics_singleton_reset_to_none
    ) -> None:
        """Booting the real app lifespan (which runs lifespan.py's Bug #532
        diagnostics backend injection) must not raise AttributeError when
        diagnostics_service starts out as None.
        """
        # This is the exact reproduction: prior to the fix, entering the
        # TestClient context manager (which runs the real lifespan) raised
        # AttributeError: 'NoneType' object has no attribute '_backend'.
        with TestClient(app):
            pass

    def test_real_lifespan_boot_constructs_and_injects_real_backend(
        self, diagnostics_singleton_reset_to_none
    ) -> None:
        """Real startup must not just avoid crashing -- it must actually
        construct the singleton and inject the SAME real DiagnosticsBackend
        instance that lifespan wired into app.state.backend_registry,
        preserving Bug #532's original behavior exactly (not merely a
        non-None placeholder)."""
        with TestClient(app):
            svc = diagnostics_router_module.diagnostics_service
            assert svc is not None, (
                "Real lifespan startup must construct the diagnostics "
                "singleton via the Bug #532 injection path instead of "
                "leaving it None."
            )

            # Reference the module-level `app` directly (not
            # `client.app`, which TestClient types as a bare ASGI
            # callable without a `.state` attribute under mypy).
            backend_registry = app.state.backend_registry
            assert backend_registry is not None
            assert hasattr(backend_registry, "diagnostics")

            assert svc._backend is backend_registry.diagnostics, (
                "Bug #532: lifespan must inject the SAME DiagnosticsBackend "
                "instance from app.state.backend_registry.diagnostics into "
                f"the diagnostics singleton. Got svc._backend={svc._backend!r}, "
                f"backend_registry.diagnostics={backend_registry.diagnostics!r}"
            )
