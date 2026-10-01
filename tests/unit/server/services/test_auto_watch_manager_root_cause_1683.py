"""
Bug #1683 (rounds 3-4): root-cause regression tests for
`AutoWatchManager.start_watch` itself.

Round 2 guarded a SINGLE caller (`mcp/handlers/files.py`'s
`_start_auto_watch_if_needed`) with a `Path.is_dir()` existence check.
Review rejected that fix as being at the wrong layer: the actual root
cause is inside `AutoWatchManager.start_watch`
(`services/auto_watch_manager.py`), which calls
`ConfigManager.create_with_backtrack(Path(repo_path))` and unconditionally
trusts the result -- even when NO config was found for `repo_path` at all.
`ConfigManager.create_with_backtrack` silently defaults to a
`{start_dir}/.code-indexer/config.json` path that does not exist, and
`ConfigManager.load()` then falls back to a bare `Config()` whose
`codebase_dir` is the unresolved relative `Path(".")` -- which resolves
against the SERVER PROCESS's CWD, not `repo_path`.

Guarding one caller (`files.py`) leaves this primitive unsafe for every
OTHER current or future caller. These tests exercise `start_watch`
directly to prove the root-cause fix protects the primitive itself.

Round 4: round 3's containment check EXPLICITLY PERMITTED
`config.codebase_dir` to be a strict ANCESTOR of `repo_path`
(`resolved_repo_path.is_relative_to(resolved_codebase_dir)`). That is the
unsafe direction, not a safe widening: `ConfigManager.find_config_path`
walks UP from `repo_path` looking for `.code-indexer/config.json`, so a
repo lacking its own config but sitting under an ancestor that happens to
carry one (e.g. the server data root) would sail through the round-3
check and then have `DaemonWatchManager` watch/index the ANCESTOR instead
of the requested repo -- the exact same "silently substituted fallback
location" defect class this bug is about, via a different trigger. Fixed
by tightening the check to strict equality
(`resolved_repo_path != resolved_codebase_dir`); the only production
callers pass an activated-repo (or write-exception canonical) root whose
own config lives directly at `<root>/.code-indexer/`, so
`codebase_dir == repo_path` holds for every legitimate case.

IMPORTANT: the round-3 implementer had ALREADY discovered this exact
ancestor-bypass condition live on a real dev machine (a genuine
`/tmp/.code-indexer/config.json` with `codebase_dir: "/tmp"`) and
suppressed it by mocking `ConfigManager.find_config_path` to return
`None`, rather than fixing the containment check. That mock is REMOVED
here. The tests below that need "no config found anywhere" semantics
build their own isolated directory tree under `~/.tmp` (never `/tmp` --
see project tmp-file convention) via `tempfile.mkdtemp`, independent of
pytest's `tmp_path` fixture (whose base directory nests under `/tmp` by
default and would otherwise pick up the real `/tmp/.code-indexer/`
ancestor on this machine), so the scenario is unambiguous regardless of
machine state or how pytest is invoked.
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from code_indexer.config import ConfigManager
from code_indexer.server.services.auto_watch_manager import AutoWatchManager


@pytest.fixture
def isolated_tmp_root():
    """A temp directory tree rooted under `~/.tmp` (never `/tmp` -- project
    convention), immune to any real `.code-indexer/config.json` that may
    exist as an ancestor of the system `/tmp` (confirmed to genuinely
    exist on this dev machine, which is exactly the discriminating
    condition for Bug #1683 round 4). Cleaned up unconditionally.
    """
    base = Path.home() / ".tmp"
    base.mkdir(parents=True, exist_ok=True)
    root = Path(tempfile.mkdtemp(dir=str(base), prefix="test_1683_"))
    try:
        yield root
    finally:
        shutil.rmtree(root, ignore_errors=True)


class TestStartWatchRefusesWhenNoConfigFoundAnywhere:
    """No `.code-indexer/config.json` for repo_path or any parent
    directory -- start_watch must fail loud, never fall back to a
    default config that would resolve codebase_dir onto the CWD.

    Uses `isolated_tmp_root` (rooted under `~/.tmp`, never `/tmp`) instead
    of mocking `ConfigManager.find_config_path`: mocking away the
    directory walk would also suppress the exact real-ancestor-config
    condition that Bug #1683 round 4 was found through (a genuine
    `/tmp/.code-indexer/config.json` on this dev machine) -- see the
    module docstring for the full history of that suppression.
    """

    def test_returns_error_status_and_never_constructs_daemon_watch_manager(
        self, isolated_tmp_root
    ) -> None:
        manager = AutoWatchManager()
        repo_path = isolated_tmp_root / "never-indexed-repo"
        repo_path.mkdir()

        with patch(
            "code_indexer.server.services.auto_watch_manager.DaemonWatchManager"
        ) as mock_daemon:
            result = manager.start_watch(str(repo_path))

        assert result["status"] == "error"
        # The daemon watch manager -- the thing that would actually start
        # watching/indexing a directory -- must never be constructed.
        mock_daemon.assert_not_called()
        assert manager.is_watching(str(repo_path)) is False

    def test_logs_warning_with_app_general_068(self, isolated_tmp_root, caplog) -> None:
        manager = AutoWatchManager()
        repo_path = isolated_tmp_root / "never-indexed-repo"
        repo_path.mkdir()

        with patch(
            "code_indexer.server.services.auto_watch_manager.DaemonWatchManager"
        ):
            with caplog.at_level("WARNING"):
                manager.start_watch(str(repo_path))

        assert any("APP-GENERAL-068" in record.message for record in caplog.records)


@pytest.fixture
def repo_path_with_foreign_codebase_dir_override(tmp_path, monkeypatch):
    """A real repo_path plus a real, unrelated `.code-indexer/config.json`
    pointed to by a `CODEBASE_DIR` env-var override.

    Reproduces a realistic (if unusual) way `create_with_backtrack` can
    resolve to a config totally unrelated to `repo_path`: the env-var
    override takes priority over the repo_path-based directory walk and
    ignores repo_path entirely.

    Returns the `repo_path` string to pass to `start_watch`.
    """
    repo_path = tmp_path / "real-activated-repo"
    repo_path.mkdir()

    unrelated_dir = tmp_path / "totally-unrelated-directory"
    unrelated_dir.mkdir()
    unrelated_config_manager = ConfigManager(
        unrelated_dir / ".code-indexer" / "config.json"
    )
    unrelated_config_manager.create_default_config(codebase_dir=unrelated_dir)

    monkeypatch.setenv("CODEBASE_DIR", str(unrelated_dir))

    return str(repo_path)


class TestStartWatchRefusesWhenCodebaseDirDoesNotMatchRepoPath:
    """A real config IS found (config_path.exists() is True), but its
    resolved codebase_dir points at an unrelated directory. start_watch
    must refuse rather than silently watch/index the wrong directory.
    """

    def test_returns_error_status_and_never_constructs_daemon_watch_manager(
        self, repo_path_with_foreign_codebase_dir_override
    ) -> None:
        manager = AutoWatchManager()
        repo_path = repo_path_with_foreign_codebase_dir_override

        with patch(
            "code_indexer.server.services.auto_watch_manager.DaemonWatchManager"
        ) as mock_daemon:
            result = manager.start_watch(repo_path)

        assert result["status"] == "error"
        mock_daemon.assert_not_called()
        assert manager.is_watching(repo_path) is False

    def test_logs_warning_with_app_general_069(
        self, repo_path_with_foreign_codebase_dir_override, caplog
    ) -> None:
        manager = AutoWatchManager()
        repo_path = repo_path_with_foreign_codebase_dir_override

        with patch(
            "code_indexer.server.services.auto_watch_manager.DaemonWatchManager"
        ):
            with caplog.at_level("WARNING"):
                manager.start_watch(repo_path)

        assert any("APP-GENERAL-069" in record.message for record in caplog.records)


@pytest.fixture
def repo_path_nested_under_ancestor_only_config(isolated_tmp_root):
    """`repo_path` has NO `.code-indexer/config.json` of its own, but a
    real ANCESTOR directory does, and that ancestor config's
    `codebase_dir` points at the ANCESTOR itself (not at `repo_path`).

    This is the exact discriminating shape for Bug #1683 round 4: round
    3's containment check explicitly permitted it (the `is_relative_to`
    branch), letting `start_watch` proceed to watch/index the ancestor.
    Built entirely on `isolated_tmp_root` (real ancestor config, real
    nested repo dir) rather than mocking `find_config_path` -- mocking it
    away would suppress this exact scenario, which is what round 3 did.

    Returns the `repo_path` string to pass to `start_watch`.
    """
    ancestor_dir = isolated_tmp_root / "server-data"
    ancestor_dir.mkdir()
    ancestor_config_manager = ConfigManager(
        ancestor_dir / ".code-indexer" / "config.json"
    )
    ancestor_config_manager.create_default_config(codebase_dir=ancestor_dir)

    # Nested UNDER the ancestor but has no config of its own --
    # find_config_path will walk up and find the ancestor's.
    repo_path = ancestor_dir / "activated-repos" / "user1" / "myrepo"
    repo_path.mkdir(parents=True)

    return str(repo_path)


class TestStartWatchRefusesWhenOnlyAncestorConfigFound:
    """Bug #1683 round 4's actual discriminating regression test -- see
    `repo_path_nested_under_ancestor_only_config` fixture docstring for
    the full scenario rationale."""

    def test_returns_error_status_and_never_constructs_daemon_watch_manager(
        self, repo_path_nested_under_ancestor_only_config
    ) -> None:
        manager = AutoWatchManager()
        repo_path = repo_path_nested_under_ancestor_only_config

        with patch(
            "code_indexer.server.services.auto_watch_manager.DaemonWatchManager"
        ) as mock_daemon:
            result = manager.start_watch(repo_path)

        assert result["status"] == "error"
        # The daemon watch manager must never be constructed -- if it
        # were, it would watch/index the ancestor, not repo_path.
        mock_daemon.assert_not_called()
        assert manager.is_watching(repo_path) is False

    def test_logs_warning_with_app_general_069(
        self, repo_path_nested_under_ancestor_only_config, caplog
    ) -> None:
        manager = AutoWatchManager()
        repo_path = repo_path_nested_under_ancestor_only_config

        with patch(
            "code_indexer.server.services.auto_watch_manager.DaemonWatchManager"
        ):
            with caplog.at_level("WARNING"):
                manager.start_watch(repo_path)

        assert any("APP-GENERAL-069" in record.message for record in caplog.records)


class TestStartWatchSucceedsWhenConfigGenuinelyMatchesRepoPath:
    """Positive control: a repo with its own real
    `.code-indexer/config.json` whose codebase_dir equals repo_path must
    still start a watch as before -- the two new guards must not
    introduce a false-negative regression."""

    def test_starts_watch_via_daemon_watch_manager(self, tmp_path) -> None:
        manager = AutoWatchManager()
        repo_path = tmp_path / "genuinely-activated-repo"
        repo_path.mkdir()

        config_manager = ConfigManager(repo_path / ".code-indexer" / "config.json")
        config_manager.create_default_config(codebase_dir=repo_path)

        with patch(
            "code_indexer.server.services.auto_watch_manager.DaemonWatchManager"
        ) as mock_daemon:
            mock_watch_instance = MagicMock()
            mock_watch_instance.start_watch.return_value = {"status": "success"}
            mock_daemon.return_value = mock_watch_instance

            result = manager.start_watch(str(repo_path))

        assert result["status"] == "success"
        mock_watch_instance.start_watch.assert_called_once()
        assert manager.is_watching(str(repo_path)) is True
