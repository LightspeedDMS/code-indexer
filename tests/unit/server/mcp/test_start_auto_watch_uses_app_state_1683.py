"""
Bug #1683: `files.py`'s `_start_auto_watch_if_needed()` resolved
`ActivatedRepoManager` via a fresh, unwired construction -- the same
anti-pattern Bug #1670 fixed in `web/routes.py`.

Confirmed live: `ActivatedRepoManager()`'s constructor hardcodes
`Path.home()/".cidx-server"/"data"` and IGNORES `CIDX_SERVER_DATA_DIR`. In
an isolated test server instance, this made the auto-watch feature look
for the activated repo at the WRONG (real dev-server) data dir, find
nothing, and (via the caller's own fallback) mis-trigger indexing on the
current working directory -- the code-indexer project tree itself.

Fix: resolve the DI-wired singleton from app.state via
`_utils._get_activated_repo_manager()` (the established Bug #1533
pattern already used elsewhere in this same module), mirroring the fix
approach in `tests/unit/server/web/test_get_activated_repo_manager_uses_app_state_1670.py`.

This test proves the fix two ways:
1. A sentinel manager installed on `app.state.activated_repo_manager`
   (configured with a data dir DIFFERENT from the hardcoded default) is
   the one actually consulted -- its `get_activated_repo_path` return
   value is what reaches `auto_watch_manager.start_watch`.
2. `ActivatedRepoManager.__init__` is patched to raise if invoked at all,
   proving the fixed code path never falls back to constructing a fresh,
   unwired instance.
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.repositories.activated_repo_manager import (
    ActivatedRepoManager,
)


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


def _make_user() -> User:
    return User(
        username="poweruser",
        password_hash="hashed_password",
        role=UserRole.POWER_USER,
        email="power@example.com",
        created_at=datetime.now(),
    )


class TestStartAutoWatchResolvesFromAppState:
    """`_start_auto_watch_if_needed` must resolve ActivatedRepoManager via
    the DI-wired app.state singleton, never a fresh construction."""

    def test_uses_app_state_activated_repo_manager_not_fresh_instance(
        self, app_state_activated_repo_manager_slot, tmp_path
    ) -> None:
        from code_indexer.server.mcp.handlers.files import (
            _start_auto_watch_if_needed,
        )

        app_module = app_state_activated_repo_manager_slot

        # Sentinel manager wired at a data dir DIFFERENT from the
        # hardcoded default (~/.cidx-server/data) -- proves resolution
        # goes through app.state, not a fresh construction pointed at
        # the wrong path. Created on disk so this test exercises ONLY the
        # round-1 DI-wiring assertion below -- the round-2 existence
        # guard (Bug #1683) is covered separately by
        # TestStartAutoWatchSkipsNonExistentResolvedPath.
        correct_repo_path = str(tmp_path / "correct-activated-repo")
        Path(correct_repo_path).mkdir(parents=True)
        sentinel_manager = MagicMock(name="sentinel-activated-repo-manager")
        sentinel_manager.get_activated_repo_path.return_value = correct_repo_path
        app_module.app.state.activated_repo_manager = sentinel_manager

        # Patch the class itself to raise if constructed -- proves the
        # fixed code path never falls back to a bare ActivatedRepoManager().
        with patch(
            "code_indexer.server.repositories.activated_repo_manager.ActivatedRepoManager.__init__",
            side_effect=AssertionError(
                "ActivatedRepoManager() must not be constructed directly; "
                "resolve via app.state singleton instead (Bug #1683)"
            ),
        ):
            with patch(
                "code_indexer.server.services.auto_watch_manager.auto_watch_manager"
            ) as mock_watch_manager:
                mock_watch_manager.start_watch = MagicMock()

                _start_auto_watch_if_needed(
                    repository_alias="some-activated-repo",
                    user=_make_user(),
                    error_code="TEST-AUTO-WATCH-1683",
                )

        # Sentinel manager was consulted with the right args.
        sentinel_manager.get_activated_repo_path.assert_called_once_with(
            username="poweruser", user_alias="some-activated-repo"
        )
        # And the watch was started on the path the sentinel returned --
        # never a path derived from a fresh, unwired construction.
        mock_watch_manager.start_watch.assert_called_once_with(correct_repo_path)

    def test_logs_warning_and_does_not_raise_when_app_state_manager_missing(
        self, app_state_activated_repo_manager_slot
    ) -> None:
        """When app.state has no wired manager, the function must degrade
        gracefully (log + return) rather than crash the caller -- matches
        the pre-existing 'auto-watch is enhancement, not critical'
        contract, and must NOT silently fall back to constructing an
        unwired ActivatedRepoManager()."""
        from code_indexer.server.mcp.handlers.files import (
            _start_auto_watch_if_needed,
        )

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
            # Must not raise -- errors are caught and logged.
            _start_auto_watch_if_needed(
                repository_alias="some-activated-repo",
                user=_make_user(),
                error_code="TEST-AUTO-WATCH-1683-MISSING",
            )


def _make_real_activated_repo_manager(tmp_path) -> ActivatedRepoManager:
    """Build a real ActivatedRepoManager for path-resolution fidelity.

    golden_repo_manager/background_job_manager are stubbed -- neither is
    touched by get_activated_repo_path (a bare os.path.join) -- purely to
    avoid the heavy real construction (SQLite golden-repo load, bgm-worker
    thread pool) that the default None args would trigger.
    """
    return ActivatedRepoManager(
        data_dir=str(tmp_path / "cidx-server-data"),
        golden_repo_manager=MagicMock(name="golden-repo-manager-stub"),
        background_job_manager=MagicMock(name="background-job-manager-stub"),
    )


class TestStartAutoWatchSkipsNonExistentResolvedPath:
    """Bug #1683 (round 2): a resolved repo path that does not exist on
    disk must never reach `auto_watch_manager.start_watch`.

    Without this guard, `ConfigManager.create_with_backtrack` finds no
    config up-tree for the non-existent path and defaults `codebase_dir`
    to `"."`, which resolves against the SERVER PROCESS's CWD --
    silently watching/indexing the server's own project tree for any
    bad/typo'd/stale `repository_alias` (confirmed live against the
    pre-fix code: a non-existent path's ConfigManager-backtracked
    `codebase_dir` resolved to the code-indexer project tree itself).
    """

    def test_skips_auto_watch_when_resolved_path_does_not_exist(
        self, app_state_activated_repo_manager_slot, tmp_path
    ) -> None:
        from code_indexer.server.mcp.handlers.files import (
            _start_auto_watch_if_needed,
        )

        app_module = app_state_activated_repo_manager_slot
        real_manager = _make_real_activated_repo_manager(tmp_path)
        app_module.app.state.activated_repo_manager = real_manager

        # Sanity: the alias was never activated, so the real manager
        # resolves it to a path that genuinely does not exist on disk.
        resolved_path = real_manager.get_activated_repo_path(
            username="poweruser", user_alias="never-activated-typo"
        )
        assert not Path(resolved_path).is_dir()

        with patch(
            "code_indexer.server.services.auto_watch_manager.auto_watch_manager"
        ) as mock_watch_manager:
            mock_watch_manager.start_watch = MagicMock()

            _start_auto_watch_if_needed(
                repository_alias="never-activated-typo",
                user=_make_user(),
                error_code="TEST-AUTO-WATCH-1683-NONEXISTENT",
            )

        mock_watch_manager.start_watch.assert_not_called()

    def test_logs_warning_when_resolved_path_does_not_exist(
        self, app_state_activated_repo_manager_slot, tmp_path, caplog
    ) -> None:
        from code_indexer.server.mcp.handlers.files import (
            _start_auto_watch_if_needed,
        )

        app_module = app_state_activated_repo_manager_slot
        real_manager = _make_real_activated_repo_manager(tmp_path)
        app_module.app.state.activated_repo_manager = real_manager

        with patch(
            "code_indexer.server.services.auto_watch_manager.auto_watch_manager"
        ) as mock_watch_manager:
            mock_watch_manager.start_watch = MagicMock()

            with caplog.at_level("WARNING"):
                _start_auto_watch_if_needed(
                    repository_alias="never-activated-typo",
                    user=_make_user(),
                    error_code="TEST-AUTO-WATCH-1683-NONEXISTENT-LOG",
                )

        assert any(
            "MCP-GENERAL-220" in record.message
            and "never-activated-typo" in record.message
            for record in caplog.records
        )

    def test_starts_watch_normally_when_resolved_path_exists(
        self, app_state_activated_repo_manager_slot, tmp_path
    ) -> None:
        """Control case: an activated repo whose directory genuinely
        exists on disk must still trigger auto-watch as before -- the
        new guard must not introduce a false-negative regression."""
        from code_indexer.server.mcp.handlers.files import (
            _start_auto_watch_if_needed,
        )

        app_module = app_state_activated_repo_manager_slot
        real_manager = _make_real_activated_repo_manager(tmp_path)
        app_module.app.state.activated_repo_manager = real_manager

        existing_alias = "genuinely-activated-repo"
        resolved_path = real_manager.get_activated_repo_path(
            username="poweruser", user_alias=existing_alias
        )
        Path(resolved_path).mkdir(parents=True, exist_ok=True)

        with patch(
            "code_indexer.server.services.auto_watch_manager.auto_watch_manager"
        ) as mock_watch_manager:
            mock_watch_manager.start_watch = MagicMock()

            _start_auto_watch_if_needed(
                repository_alias=existing_alias,
                user=_make_user(),
                error_code="TEST-AUTO-WATCH-1683-EXISTS",
            )

        mock_watch_manager.start_watch.assert_called_once_with(resolved_path)
