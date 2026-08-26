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
        sentinel_manager.user_has_activated_repo.return_value = True
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
        sentinel_manager.user_has_activated_repo.assert_called_once_with(
            "poweruser", "some-activated-repo"
        )
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


def _register_activated_repo(
    manager: ActivatedRepoManager, username: str, user_alias: str
) -> None:
    """Write real activation metadata so `user_has_activated_repo` sees it.

    Mirrors the on-disk shape `ActivatedRepoManager.activate_repository`
    produces (a `{alias}_metadata.json` file under the user's directory),
    without paying for the full activation workflow (golden repo lookup,
    CoW clone, background job).
    """
    manager._save_metadata(
        username, user_alias, {"user_alias": user_alias, "golden_repo_alias": "src"}
    )


class TestStartAutoWatchSkipsNonExistentResolvedPath:
    """Bug #1683 (round 2, superseded by round 3): a resolved repo path
    that does not exist on disk must never reach
    `auto_watch_manager.start_watch`.

    Without a guard, `ConfigManager.create_with_backtrack` finds no
    config up-tree for the non-existent path and defaults `codebase_dir`
    to `"."`, which resolves against the SERVER PROCESS's CWD --
    silently watching/indexing the server's own project tree for any
    bad/typo'd/stale `repository_alias` (confirmed live against the
    pre-fix code: a non-existent path's ConfigManager-backtracked
    `codebase_dir` resolved to the code-indexer project tree itself).

    Round 3 replaced the primary guard with
    `user_has_activated_repo` (see `TestStartAutoWatchSkipsExistsButUnindexedOrphan`
    and `TestStartAutoWatchSelfDefeatingGuardClosed` below for the cases
    that motivated the change); the `is_dir()` check exercised here is
    retained as defense-in-depth and still fires for a genuinely
    non-existent alias since it is unregistered too.
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
            "MCP-GENERAL-223" in record.message
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
        _register_activated_repo(real_manager, "poweruser", existing_alias)
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


class TestStartAutoWatchSkipsExistsButUnindexedOrphan:
    """Bug #1683 (round 3, BLOCKER 1): an activated-repo directory that
    EXISTS on disk but was never actually activated (an orphan clone from
    a partially-failed activation, or a repo deactivated on another
    cluster node) must still be refused -- matching the real dev-server
    `admin/tstwiki` shape (directory exists, no registry entry).

    Round 2's `Path.is_dir()` guard sailed straight through this case
    (`is_dir: True`) and still reached `auto_watch_manager.start_watch`
    with the server's own CWD as the eventual `codebase_dir`. The round-3
    guard (`user_has_activated_repo`) is registry-backed, not
    filesystem-backed, so it correctly refuses regardless of what exists
    on disk at the computed path.
    """

    def test_skips_auto_watch_for_orphan_directory_that_exists_but_is_unregistered(
        self, app_state_activated_repo_manager_slot, tmp_path
    ) -> None:
        from code_indexer.server.mcp.handlers.files import (
            _start_auto_watch_if_needed,
        )

        app_module = app_state_activated_repo_manager_slot
        real_manager = _make_real_activated_repo_manager(tmp_path)
        app_module.app.state.activated_repo_manager = real_manager

        orphan_alias = "tstwiki"
        orphan_path = real_manager.get_activated_repo_path(
            username="admin", user_alias=orphan_alias
        )
        # The orphan directory genuinely EXISTS on disk (unlike round 2's
        # non-existent-path scenario) -- e.g. left behind by a
        # partially-failed activation -- but no `{alias}_metadata.json`
        # was ever written, so it is NOT in the activation registry.
        Path(orphan_path).mkdir(parents=True, exist_ok=True)
        assert Path(orphan_path).is_dir()  # sanity: is_dir() alone would pass
        assert not real_manager.user_has_activated_repo("admin", orphan_alias)

        with patch(
            "code_indexer.server.services.auto_watch_manager.auto_watch_manager"
        ) as mock_watch_manager:
            mock_watch_manager.start_watch = MagicMock()

            _start_auto_watch_if_needed(
                repository_alias=orphan_alias,
                user=User(
                    username="admin",
                    password_hash="hashed_password",
                    role=UserRole.ADMIN,
                    email="admin@example.com",
                    created_at=datetime.now(),
                ),
                error_code="TEST-AUTO-WATCH-1683-ORPHAN",
            )

        mock_watch_manager.start_watch.assert_not_called()

    def test_logs_warning_for_orphan_directory(
        self, app_state_activated_repo_manager_slot, tmp_path, caplog
    ) -> None:
        from code_indexer.server.mcp.handlers.files import (
            _start_auto_watch_if_needed,
        )

        app_module = app_state_activated_repo_manager_slot
        real_manager = _make_real_activated_repo_manager(tmp_path)
        app_module.app.state.activated_repo_manager = real_manager

        orphan_alias = "tstwiki"
        orphan_path = real_manager.get_activated_repo_path(
            username="admin", user_alias=orphan_alias
        )
        Path(orphan_path).mkdir(parents=True, exist_ok=True)

        with patch(
            "code_indexer.server.services.auto_watch_manager.auto_watch_manager"
        ) as mock_watch_manager:
            mock_watch_manager.start_watch = MagicMock()

            with caplog.at_level("WARNING"):
                _start_auto_watch_if_needed(
                    repository_alias=orphan_alias,
                    user=_make_user(),
                    error_code="TEST-AUTO-WATCH-1683-ORPHAN-LOG",
                )

        assert any(
            "MCP-GENERAL-223" in record.message and orphan_alias in record.message
            for record in caplog.records
        )


class TestStartAutoWatchSelfDefeatingGuardClosed:
    """Bug #1683 (round 3, BLOCKER 2): the round-2 `is_dir()` guard was
    self-defeating -- the FIRST errant `start_watch` call for a
    non-existent alias materializes `<repo_path>/.code-indexer/index` as
    a side effect (via `BackendFactory...get_vector_store_client()`,
    downstream of `DaemonWatchManager.start_watch`), so `is_dir()` then
    returns True on every subsequent call and the guard permanently
    disables itself for that alias.

    This test simulates exactly that residue -- a `.code-indexer/index`
    directory pre-created under an otherwise-unregistered alias -- and
    proves the round-3 guard (registry-backed, not filesystem-backed)
    still correctly refuses, since it does not depend on any on-disk
    state a prior errant run could have polluted.
    """

    def test_refuses_even_when_prior_errant_run_created_index_directory(
        self, app_state_activated_repo_manager_slot, tmp_path
    ) -> None:
        from code_indexer.server.mcp.handlers.files import (
            _start_auto_watch_if_needed,
        )

        app_module = app_state_activated_repo_manager_slot
        real_manager = _make_real_activated_repo_manager(tmp_path)
        app_module.app.state.activated_repo_manager = real_manager

        alias = "polluted-by-prior-errant-run"
        repo_path = real_manager.get_activated_repo_path(
            username="poweruser", user_alias=alias
        )
        # Simulate the side effect a prior errant `start_watch` call would
        # have left behind -- a real .code-indexer/index directory -- with
        # NO activation metadata ever written (the alias was never truly
        # activated).
        (Path(repo_path) / ".code-indexer" / "index").mkdir(parents=True)
        assert Path(repo_path).is_dir()  # is_dir() alone is now satisfied
        assert not real_manager.user_has_activated_repo("poweruser", alias)

        with patch(
            "code_indexer.server.services.auto_watch_manager.auto_watch_manager"
        ) as mock_watch_manager:
            mock_watch_manager.start_watch = MagicMock()

            _start_auto_watch_if_needed(
                repository_alias=alias,
                user=_make_user(),
                error_code="TEST-AUTO-WATCH-1683-SELF-DEFEATING",
            )

        mock_watch_manager.start_watch.assert_not_called()
