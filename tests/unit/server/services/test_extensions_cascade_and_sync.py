"""
Unit tests for ConfigService cascade and sync methods (Story #223 - AC5, AC6, AC7).

Tests:
- cascade_indexable_extensions_to_repos() (AC5)
- seed_repo_extensions_from_server_config() (AC6)
- sync_repo_extensions_if_drifted() (AC7)

TDD: These tests are written BEFORE implementation to define expected behavior.
"""

import json
import os
import shutil
import tempfile
from pathlib import Path
from unittest.mock import MagicMock, patch

from code_indexer.server.services.config_service import ConfigService, reset_config_service


def _write_cidx_config(repo_path: str, file_extensions: list, extra: dict = None):
    """Helper: write a .code-indexer/config.json in repo_path."""
    cidx_dir = Path(repo_path) / ".code-indexer"
    cidx_dir.mkdir(parents=True, exist_ok=True)
    config = {"file_extensions": file_extensions}
    if extra:
        config.update(extra)
    with open(cidx_dir / "config.json", "w") as f:
        json.dump(config, f)


def _read_cidx_config(repo_path: str) -> dict:
    """Helper: read .code-indexer/config.json from repo_path."""
    cidx_config_path = Path(repo_path) / ".code-indexer" / "config.json"
    with open(cidx_config_path, "r") as f:
        return json.load(f)


class TestCascadeToGoldenRepos:
    """Tests for cascade_indexable_extensions_to_repos() (AC5)."""

    def setup_method(self):
        """Setup temp dirs and config service."""
        self.temp_dir = tempfile.mkdtemp()
        self.repo1_dir = tempfile.mkdtemp()
        self.repo2_dir = tempfile.mkdtemp()
        self.config_service = ConfigService(server_dir_path=self.temp_dir)
        self.config_service.load_config()

    def teardown_method(self):
        """Clean up."""
        reset_config_service()
        for d in [self.temp_dir, self.repo1_dir, self.repo2_dir]:
            if os.path.exists(d):
                shutil.rmtree(d)

    def _make_mock_manager(self, repos, paths):
        """Helper: create mock golden repo manager."""
        manager = MagicMock()
        manager.list_golden_repos.return_value = repos
        manager.get_actual_repo_path.side_effect = lambda alias: paths.get(alias, "")
        return manager

    def test_cascade_updates_file_extensions_in_repo_config(self):
        """AC5: cascade must write server extensions to each repo's config.json."""
        _write_cidx_config(self.repo1_dir, ["old_ext"])
        server_exts = [".py", ".go", ".ts"]
        self.config_service.update_setting("indexing", "indexable_extensions", server_exts)

        repos = [{"alias": "repo1"}]
        mock_manager = self._make_mock_manager(repos, {"repo1": self.repo1_dir})

        with patch(
            "code_indexer.server.repositories.golden_repo_manager.get_golden_repo_manager",
            return_value=mock_manager,
        ):
            self.config_service.cascade_indexable_extensions_to_repos()

        result = _read_cidx_config(self.repo1_dir)
        assert result["file_extensions"] == ["py", "go", "ts"]

    def test_cascade_replaces_old_extensions(self):
        """AC5: cascade must replace, not append to, existing file_extensions."""
        _write_cidx_config(self.repo1_dir, ["old1", "old2", "old3"])
        server_exts = [".rs"]
        self.config_service.update_setting("indexing", "indexable_extensions", server_exts)

        repos = [{"alias": "repo1"}]
        mock_manager = self._make_mock_manager(repos, {"repo1": self.repo1_dir})

        with patch(
            "code_indexer.server.repositories.golden_repo_manager.get_golden_repo_manager",
            return_value=mock_manager,
        ):
            self.config_service.cascade_indexable_extensions_to_repos()

        result = _read_cidx_config(self.repo1_dir)
        assert result["file_extensions"] == ["rs"]

    def test_cascade_strips_leading_dots_for_cli_format(self):
        """AC5: cascade must strip leading dots since CLI format has no dots."""
        _write_cidx_config(self.repo1_dir, [])
        server_exts = [".py", ".go"]
        self.config_service.update_setting("indexing", "indexable_extensions", server_exts)

        repos = [{"alias": "repo1"}]
        mock_manager = self._make_mock_manager(repos, {"repo1": self.repo1_dir})

        with patch(
            "code_indexer.server.repositories.golden_repo_manager.get_golden_repo_manager",
            return_value=mock_manager,
        ):
            self.config_service.cascade_indexable_extensions_to_repos()

        result = _read_cidx_config(self.repo1_dir)
        for ext in result["file_extensions"]:
            assert not ext.startswith("."), f"Extension {ext!r} should not have leading dot"

    def test_cascade_preserves_other_config_fields(self):
        """AC5: cascade must not overwrite other fields in repo config.json."""
        _write_cidx_config(
            self.repo1_dir,
            ["py"],
            extra={"embedding_provider": "voyage-ai", "collection": "my-repo"},
        )
        server_exts = [".py", ".ts"]
        self.config_service.update_setting("indexing", "indexable_extensions", server_exts)

        repos = [{"alias": "repo1"}]
        mock_manager = self._make_mock_manager(repos, {"repo1": self.repo1_dir})

        with patch(
            "code_indexer.server.repositories.golden_repo_manager.get_golden_repo_manager",
            return_value=mock_manager,
        ):
            self.config_service.cascade_indexable_extensions_to_repos()

        result = _read_cidx_config(self.repo1_dir)
        assert result["embedding_provider"] == "voyage-ai"
        assert result["collection"] == "my-repo"

    def test_cascade_continues_on_individual_repo_failure(self):
        """AC5: cascade must continue to next repo if one fails."""
        _write_cidx_config(self.repo2_dir, ["old"])
        server_exts = [".py"]
        self.config_service.update_setting("indexing", "indexable_extensions", server_exts)

        repos = [{"alias": "repo1"}, {"alias": "repo2"}]
        # repo1 has an invalid path (no .code-indexer/config.json) - must not crash
        mock_manager = self._make_mock_manager(
            repos,
            {"repo1": "/nonexistent/path/that/does/not/exist", "repo2": self.repo2_dir},
        )

        with patch(
            "code_indexer.server.repositories.golden_repo_manager.get_golden_repo_manager",
            return_value=mock_manager,
        ):
            # Must not raise exception
            self.config_service.cascade_indexable_extensions_to_repos()

        # repo2 must have been updated
        result = _read_cidx_config(self.repo2_dir)
        assert result["file_extensions"] == ["py"]

    def test_cascade_does_nothing_when_no_repos(self):
        """AC5: cascade with empty repo list must complete without error."""
        server_exts = [".py"]
        self.config_service.update_setting("indexing", "indexable_extensions", server_exts)

        mock_manager = MagicMock()
        mock_manager.list_golden_repos.return_value = []

        with patch(
            "code_indexer.server.repositories.golden_repo_manager.get_golden_repo_manager",
            return_value=mock_manager,
        ):
            self.config_service.cascade_indexable_extensions_to_repos()

        # No assertion needed - just verifies no exception raised


class TestSeedNewRepoFromServerConfig:
    """Tests for seed_repo_extensions_from_server_config() (AC6)."""

    def setup_method(self):
        """Setup temp dirs and config service."""
        self.temp_dir = tempfile.mkdtemp()
        self.repo_dir = tempfile.mkdtemp()
        self.config_service = ConfigService(server_dir_path=self.temp_dir)
        self.config_service.load_config()

    def teardown_method(self):
        """Clean up."""
        reset_config_service()
        for d in [self.temp_dir, self.repo_dir]:
            if os.path.exists(d):
                shutil.rmtree(d)

    def test_seed_overwrites_cidx_init_defaults(self):
        """AC6: seed must overwrite the cidx-init defaults with server config."""
        # cidx init creates default extensions
        _write_cidx_config(self.repo_dir, ["py", "js", "ts"])
        server_exts = [".rs", ".go"]
        self.config_service.update_setting("indexing", "indexable_extensions", server_exts)

        self.config_service.seed_repo_extensions_from_server_config(self.repo_dir)

        result = _read_cidx_config(self.repo_dir)
        assert result["file_extensions"] == ["rs", "go"]

    def test_seed_strips_leading_dots(self):
        """AC6: seed must strip leading dots for CLI format."""
        _write_cidx_config(self.repo_dir, ["py"])
        server_exts = [".py", ".ts"]
        self.config_service.update_setting("indexing", "indexable_extensions", server_exts)

        self.config_service.seed_repo_extensions_from_server_config(self.repo_dir)

        result = _read_cidx_config(self.repo_dir)
        for ext in result["file_extensions"]:
            assert not ext.startswith("."), f"Extension {ext!r} should not have leading dot"

    def test_seed_handles_missing_cidx_config_gracefully(self):
        """AC6: seed must not raise if .code-indexer/config.json does not exist."""
        # No .code-indexer dir created
        server_exts = [".py"]
        self.config_service.update_setting("indexing", "indexable_extensions", server_exts)

        # Must not raise
        self.config_service.seed_repo_extensions_from_server_config(self.repo_dir)

    def test_seed_preserves_other_config_fields(self):
        """AC6: seed must not overwrite other fields in config.json."""
        _write_cidx_config(
            self.repo_dir,
            ["py"],
            extra={"embedding_provider": "voyage-ai", "model": "voyage-3"},
        )
        server_exts = [".rs"]
        self.config_service.update_setting("indexing", "indexable_extensions", server_exts)

        self.config_service.seed_repo_extensions_from_server_config(self.repo_dir)

        result = _read_cidx_config(self.repo_dir)
        assert result["embedding_provider"] == "voyage-ai"
        assert result["model"] == "voyage-3"
        assert result["file_extensions"] == ["rs"]


class TestRefreshSyncCorrectsDrift:
    """Tests for sync_repo_extensions_if_drifted() (AC7)."""

    def setup_method(self):
        """Setup temp dirs and config service."""
        self.temp_dir = tempfile.mkdtemp()
        self.repo_dir = tempfile.mkdtemp()
        self.config_service = ConfigService(server_dir_path=self.temp_dir)
        self.config_service.load_config()

    def teardown_method(self):
        """Clean up."""
        reset_config_service()
        for d in [self.temp_dir, self.repo_dir]:
            if os.path.exists(d):
                shutil.rmtree(d)

    def test_sync_corrects_drifted_repo_config(self):
        """AC7: sync must update repo config when extensions have drifted."""
        # Repo has old extensions
        _write_cidx_config(self.repo_dir, ["py", "js"])
        # Server now has different extensions
        server_exts = [".py", ".ts", ".go"]
        self.config_service.update_setting("indexing", "indexable_extensions", server_exts)

        self.config_service.sync_repo_extensions_if_drifted(self.repo_dir)

        result = _read_cidx_config(self.repo_dir)
        assert result["file_extensions"] == ["py", "ts", "go"]

    def test_sync_does_not_rewrite_when_already_in_sync(self):
        """AC7: sync must not modify the file when extensions already match."""
        server_exts = [".py", ".ts"]
        self.config_service.update_setting("indexing", "indexable_extensions", server_exts)
        # Write the same extensions to repo (already in sync)
        _write_cidx_config(self.repo_dir, ["py", "ts"])

        cidx_config_path = Path(self.repo_dir) / ".code-indexer" / "config.json"
        mtime_before = cidx_config_path.stat().st_mtime

        self.config_service.sync_repo_extensions_if_drifted(self.repo_dir)

        mtime_after = cidx_config_path.stat().st_mtime
        assert mtime_before == mtime_after, "File was rewritten when it shouldn't have been"

    def test_sync_handles_missing_repo_config_gracefully(self):
        """AC7: sync must not raise if .code-indexer/config.json does not exist."""
        server_exts = [".py"]
        self.config_service.update_setting("indexing", "indexable_extensions", server_exts)

        # Must not raise
        self.config_service.sync_repo_extensions_if_drifted(self.repo_dir)

    def test_sync_strips_dots_before_writing(self):
        """AC7: sync must write CLI format (no leading dots) to repo config."""
        _write_cidx_config(self.repo_dir, ["old_ext"])
        server_exts = [".py", ".go"]
        self.config_service.update_setting("indexing", "indexable_extensions", server_exts)

        self.config_service.sync_repo_extensions_if_drifted(self.repo_dir)

        result = _read_cidx_config(self.repo_dir)
        for ext in result["file_extensions"]:
            assert not ext.startswith("."), f"Extension {ext!r} should not have leading dot"
