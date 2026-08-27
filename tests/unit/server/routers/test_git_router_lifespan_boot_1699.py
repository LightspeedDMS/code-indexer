"""Bug #1699 / Bug #1702 regression: real server startup must survive and
correctly wire `routers/git.py`'s `_get_activated_repo_manager()`.

Bug #1699 originally fixed an import-time side effect here (a bare
`activated_repo_manager = ActivatedRepoManager(...)` running unconditionally
at module import) by deferring construction to first call via a
module-level double-checked-locking singleton. That fix deferred WHEN the
manager was built but not WHICH instance -- it still constructed its own
node-local, unpooled `ActivatedRepoManager`, entirely separate from the
DI-wired `app.state.activated_repo_manager` singleton built in
`startup/service_init.py`. Bug #1692 proved this exact shape causes a real
cluster-mode outage in `file_crud_service.py` (a node-local instance's
registry check falls back to scanning local `{alias}_metadata.json` files
that PostgreSQL/cluster-mode activation never writes).

Bug #1702 converges `_get_activated_repo_manager()` on the same DI-wired
resolution pattern already used by `routers/repository_health.py` and by
file_crud_service.py's Bug #1692 fix: it now reads
`app.state.activated_repo_manager` at call time instead of constructing
its own instance. This test boots the REAL app lifespan (mirroring
test_git_cat_endpoint.py's established `from code_indexer.server.app import
app` + `with TestClient(app):` pattern) and proves:

  1. Boot does not crash (the exact class of failure #1686 introduced and
     #1689 had to avoid repeating).
  2. `_get_activated_repo_manager()` returns the SAME object identity as
     `app.state.activated_repo_manager` -- i.e. it resolves the DI-wired
     singleton rather than constructing a separate, node-local instance.
  3. That resolved instance is a real, functional ActivatedRepoManager.
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from code_indexer.server.app import app
from code_indexer.server.repositories.activated_repo_manager import (
    ActivatedRepoManager,
)
from code_indexer.server.routers import git as git_router_module


class TestRealLifespanBootWiresAppStateResolvedActivatedRepoManager:
    """Booting the real app lifespan must not crash, and
    `_get_activated_repo_manager()` must resolve the exact DI-wired
    `app.state.activated_repo_manager` singleton -- never a separate,
    node-local instance (Bug #1702)."""

    def test_real_lifespan_boot_resolves_the_app_state_singleton(self) -> None:
        with TestClient(app):
            app_state_manager = app.state.activated_repo_manager
            assert isinstance(app_state_manager, ActivatedRepoManager), (
                "Real server startup must wire a real ActivatedRepoManager "
                f"onto app.state. Got: {type(app_state_manager)!r}"
            )

            resolved = git_router_module._get_activated_repo_manager()

            assert resolved is app_state_manager, (
                "BUG #1702 REGRESSION: routers/git.py's "
                "_get_activated_repo_manager() must resolve the SAME "
                "app.state.activated_repo_manager singleton, not construct "
                "a separate node-local instance."
            )
            assert isinstance(resolved.data_dir, str) and resolved.data_dir, (
                "The DI-wired ActivatedRepoManager must be a genuinely "
                f"usable instance (non-empty data_dir). Got: {resolved.data_dir!r}"
            )
            assert resolved is git_router_module._get_activated_repo_manager(), (
                "_get_activated_repo_manager() must return the SAME "
                "instance on repeated calls during a real server lifetime."
            )
