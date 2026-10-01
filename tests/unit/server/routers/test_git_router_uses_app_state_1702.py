"""
Bug #1702: routers/git.py's `_get_activated_repo_manager()` (lazily
constructed per #1699's fix) built its own node-local, unpooled
`ActivatedRepoManager` -- entirely separate from the DI-wired
`app.state.activated_repo_manager` singleton built in
`startup/service_init.py`. This is the SAME underlying architectural
issue as Bug #1692 (`file_crud_service.py` consulting a node-local,
unpooled `ActivatedRepoManager` instead of the shared/pooled one, which
caused a real cluster-mode outage): a duplicate instance that must never
be relied upon to agree with the cluster's shared registry state.

git.py's three handlers (`git_cat`, `git_blame`, `git_file_history`) only
ever needed a WORKING manager for path resolution, not one that agrees
with cluster registry state -- so the divergence never caused an
observable bug here, unlike #1692. But the shape is identical and
therefore fragile the moment a future change starts relying on
agreement with the shared registry.

Fix: converge `_get_activated_repo_manager()` on the same DI-wired
resolution pattern already used by `routers/repository_health.py:211`
and by file_crud_service.py's Bug #1692 fix -- resolve
`app.state.activated_repo_manager` at call time, fail loud (RuntimeError)
if the server hasn't wired it during startup. No more node-local
construction, no more module-level singleton/lock.

This test file supersedes the now-obsolete lazy-singleton-construction
assertions in test_git_router_lazy_activated_repo_manager_1699.py and
test_git_router_lifespan_boot_1699.py (both updated in the same commit
to test the new app.state-resolution behavior instead), mirroring the
established test shape from test_get_activated_repo_manager_uses_app_state_1670.py
and test_stats_service_uses_app_state_1683.py.
"""

from __future__ import annotations

from typing import Any, Generator
from unittest.mock import MagicMock

import pytest

from code_indexer.server.routers import git as git_router_module

_UNSET = object()


@pytest.fixture
def app_state_activated_repo_manager_slot() -> Generator[Any, None, None]:
    """Save/restore app.state.activated_repo_manager around a test."""
    from code_indexer.server import app as app_module

    saved = getattr(app_module.app.state, "activated_repo_manager", _UNSET)
    try:
        yield app_module
    finally:
        if saved is _UNSET:
            if hasattr(app_module.app.state, "activated_repo_manager"):
                delattr(app_module.app.state, "activated_repo_manager")
        else:
            app_module.app.state.activated_repo_manager = saved


class TestGetActivatedRepoManagerResolvesFromAppState:
    """Structural/behavioral proof the helper reads app.state, not a
    freshly-constructed node-local instance."""

    def test_returns_the_app_state_singleton(
        self, app_state_activated_repo_manager_slot
    ) -> None:
        app_module = app_state_activated_repo_manager_slot
        sentinel_manager = MagicMock(name="sentinel-activated-repo-manager")
        app_module.app.state.activated_repo_manager = sentinel_manager

        result = git_router_module._get_activated_repo_manager()

        assert result is sentinel_manager

    def test_never_constructs_a_fresh_activated_repo_manager(
        self, app_state_activated_repo_manager_slot
    ) -> None:
        """Bug #1702 core reproduction: the getter must resolve the
        DI-wired singleton, never construct its own node-local instance
        -- the exact anti-pattern that caused Bug #1692's cluster-mode
        outage in file_crud_service.py."""
        from unittest.mock import patch

        app_module = app_state_activated_repo_manager_slot
        sentinel_manager = MagicMock(name="sentinel-activated-repo-manager")
        app_module.app.state.activated_repo_manager = sentinel_manager

        with patch(
            "code_indexer.server.repositories.activated_repo_manager."
            "ActivatedRepoManager.__init__",
            side_effect=AssertionError(
                "ActivatedRepoManager() must not be constructed directly; "
                "resolve via app.state singleton instead (Bug #1702)"
            ),
        ):
            result = git_router_module._get_activated_repo_manager()

        assert result is sentinel_manager

    def test_raises_runtime_error_when_not_initialized(
        self, app_state_activated_repo_manager_slot
    ) -> None:
        app_module = app_state_activated_repo_manager_slot
        if hasattr(app_module.app.state, "activated_repo_manager"):
            delattr(app_module.app.state, "activated_repo_manager")

        with pytest.raises(
            RuntimeError, match="activated_repo_manager not initialized"
        ):
            git_router_module._get_activated_repo_manager()


class TestNoModuleLevelSingletonRemains:
    """Bug #1702 eliminates the duplicate node-local instance entirely --
    there must be no module-level `activated_repo_manager` singleton or
    lock left over from #1699's deferred-construction fix."""

    def test_no_bare_activated_repo_manager_module_attribute(self) -> None:
        assert not hasattr(git_router_module, "activated_repo_manager"), (
            "Bug #1702: routers/git.py must not retain a module-level "
            "`activated_repo_manager` singleton attribute -- resolution "
            "must go through app.state exclusively."
        )

    def test_no_activated_repo_manager_lock_attribute(self) -> None:
        assert not hasattr(git_router_module, "_activated_repo_manager_lock"), (
            "Bug #1702: routers/git.py must not retain the #1699 "
            "double-checked-locking lock -- there is nothing left to "
            "construct under a lock once resolution is a pure app.state read."
        )
