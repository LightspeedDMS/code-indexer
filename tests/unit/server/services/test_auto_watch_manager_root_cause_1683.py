"""
Bug #1683 (round 3): root-cause regression tests for
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
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from code_indexer.config import ConfigManager
from code_indexer.server.services.auto_watch_manager import AutoWatchManager


class TestStartWatchRefusesWhenNoConfigFoundAnywhere:
    """No `.code-indexer/config.json` for repo_path or any parent
    directory -- start_watch must fail loud, never fall back to a
    default config that would resolve codebase_dir onto the CWD.

    `ConfigManager.find_config_path` is patched to return None rather
    than relying on the real filesystem having no ancestor config: some
    dev machines genuinely have a `.code-indexer/config.json` directly
    under `/tmp` (a real, unrelated cidx project), and pytest's
    `tmp_path` fixture nests under `/tmp` -- without this patch the
    directory-walk would pick that up as a false ancestor match and the
    test would pass or fail depending on machine state.
    """

    def test_returns_error_status_and_never_constructs_daemon_watch_manager(
        self, tmp_path
    ) -> None:
        manager = AutoWatchManager()
        repo_path = tmp_path / "never-indexed-repo"
        repo_path.mkdir()

        with patch(
            "code_indexer.config.ConfigManager.find_config_path", return_value=None
        ):
            with patch(
                "code_indexer.server.services.auto_watch_manager.DaemonWatchManager"
            ) as mock_daemon:
                result = manager.start_watch(str(repo_path))

        assert result["status"] == "error"
        # The daemon watch manager -- the thing that would actually start
        # watching/indexing a directory -- must never be constructed.
        mock_daemon.assert_not_called()
        assert manager.is_watching(str(repo_path)) is False

    def test_logs_warning_with_app_general_068(self, tmp_path, caplog) -> None:
        manager = AutoWatchManager()
        repo_path = tmp_path / "never-indexed-repo"
        repo_path.mkdir()

        with patch(
            "code_indexer.config.ConfigManager.find_config_path", return_value=None
        ):
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
