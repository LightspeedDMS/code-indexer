"""Bug #1699 regression: real server startup must survive the lazy
`routers/git.py` `activated_repo_manager` singleton.

This mirrors the "single most important check" lesson documented for the
sibling issues #1686 and #1689: a near-identical lazy-singleton fix
(routers/diagnostics.py) was REJECTED in its first round because it broke
real server startup -- `startup/lifespan.py` did a bare-name `from module
import name` against a singleton the fix had bound to `None`, and a
downstream `_backend = ...` assignment raised `AttributeError:
'NoneType' object has no attribute '_backend'` with no enclosing
try/except, aborting the entire boot.

Consumer audit performed for Bug #1699 (see
test_git_router_lazy_activated_repo_manager_1699.py's module docstring for
the full detail) found NO `startup/lifespan.py` or `startup/service_init.py`
code that imports or reads `routers.git.activated_repo_manager` at all --
unlike diagnostics_service's Bug #532 injection or file_crud_service's
Story #197 AC1/AC4 write-exception registration. This test exists to prove
that finding empirically rather than merely asserting it from a grep: it
boots the REAL app lifespan (mirroring test_git_cat_endpoint.py's
established `from code_indexer.server.app import app` +
`with TestClient(app) as client:` pattern) with the singleton explicitly
reset to None beforehand (simulating true fresh-server state), and confirms
boot does not crash and the lazily-constructed manager is a real, working
instance afterward.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from code_indexer.server.app import app
from code_indexer.server.repositories.activated_repo_manager import (
    ActivatedRepoManager,
)
from code_indexer.server.routers import git as git_router_module


@pytest.fixture()
def activated_repo_manager_reset_to_none():
    """Explicitly save the current `activated_repo_manager` module
    attribute, set it to None for the duration of the test (simulating
    true fresh-server state), and restore the original value afterward.
    """
    original_value = git_router_module.activated_repo_manager
    git_router_module.activated_repo_manager = None
    try:
        yield
    finally:
        git_router_module.activated_repo_manager = original_value


class TestRealLifespanBootSurvivesLazyActivatedRepoManagerSingleton:
    """Booting the real app lifespan must not crash on the lazy
    `activated_repo_manager` singleton in routers/git.py, and must leave a
    fully-functional manager available afterward."""

    def test_real_lifespan_boot_produces_working_lazy_singleton(
        self, activated_repo_manager_reset_to_none
    ) -> None:
        """Booting the real app lifespan must not raise (the exact class
        of failure #1686 introduced and #1689 must not repeat), AND the
        lazy getter must still construct a real, functional
        ActivatedRepoManager during/after that real server lifetime --
        not just avoid crashing.
        """
        with TestClient(app):
            arm = git_router_module._get_activated_repo_manager()
            assert isinstance(arm, ActivatedRepoManager), (
                "BUG #1699 REGRESSION: _get_activated_repo_manager() must "
                "construct a real ActivatedRepoManager instance during/"
                f"after real server startup. Got: {type(arm)!r}"
            )
            assert isinstance(arm.data_dir, str) and arm.data_dir, (
                "The lazily-constructed ActivatedRepoManager must be a "
                "genuinely usable instance (non-empty data_dir), not a "
                f"half-initialized placeholder. Got: {arm.data_dir!r}"
            )
            assert arm is git_router_module._get_activated_repo_manager(), (
                "_get_activated_repo_manager() must return the SAME "
                "cached singleton on repeated calls during a real "
                "server lifetime."
            )
