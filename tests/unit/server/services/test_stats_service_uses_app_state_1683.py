"""
Bug #1683: `stats_service.py`'s `_get_repository_path()` resolved
`ActivatedRepoManager` via a fresh, unwired construction -- the same
anti-pattern Bug #1670 fixed in `web/routes.py` and Bug #1683 fixed in
`mcp/handlers/files.py`.

`ActivatedRepoManager()`'s constructor hardcodes
`Path.home()/".cidx-server"/"data"` and IGNORES `CIDX_SERVER_DATA_DIR`, so
in cluster mode (or any deployment overriding the data dir) it reads from
the WRONG per-node store instead of the shared, DI-wired singleton every
other activated-repo lookup path uses. This path is reachable from both
the REST front door (`routers/inline_repos_v2.py`'s
`get_repository_stats`) and the MCP front door
(`mcp/handlers/repos.py`'s `stats_service.get_repository_stats` call).

Fix: resolve the DI-wired singleton from app.state via a new
`_get_activated_repo_manager()` helper, mirroring this same file's
existing `_get_golden_repos_dir()` pattern.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.services.stats_service import stats_service


_UNSET = object()


@pytest.fixture
def app_state_activated_repo_manager_slot():
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


class TestGetRepositoryPathResolvesFromAppState:
    def test_get_repository_path_uses_app_state_activated_repo_manager(
        self, app_state_activated_repo_manager_slot, tmp_path
    ) -> None:
        app_module = app_state_activated_repo_manager_slot

        correct_repo_dir = tmp_path / "correct-activated-repo"
        correct_repo_dir.mkdir()
        sentinel_manager = MagicMock(name="sentinel-activated-repo-manager")
        sentinel_manager.get_activated_repo_path.return_value = str(correct_repo_dir)
        app_module.app.state.activated_repo_manager = sentinel_manager

        with patch(
            "code_indexer.server.repositories.activated_repo_manager.ActivatedRepoManager.__init__",
            side_effect=AssertionError(
                "ActivatedRepoManager() must not be constructed directly; "
                "resolve via app.state singleton instead (Bug #1683)"
            ),
        ):
            result = stats_service._get_repository_path(
                repo_id="some-activated-repo", username="poweruser"
            )

        assert result == str(correct_repo_dir)
        sentinel_manager.get_activated_repo_path.assert_called_once_with(
            username="poweruser", user_alias="some-activated-repo"
        )

    def test_raises_runtime_error_when_app_state_manager_missing(
        self, app_state_activated_repo_manager_slot
    ) -> None:
        app_module = app_state_activated_repo_manager_slot
        if hasattr(app_module.app.state, "activated_repo_manager"):
            delattr(app_module.app.state, "activated_repo_manager")

        with patch(
            "code_indexer.server.repositories.activated_repo_manager.ActivatedRepoManager.__init__",
            side_effect=AssertionError(
                "ActivatedRepoManager() must not be constructed directly "
                "even when app.state has no manager wired (Bug #1683)"
            ),
        ):
            with pytest.raises(RuntimeError, match="Unable to access repository"):
                stats_service._get_repository_path(
                    repo_id="some-activated-repo", username="poweruser"
                )
