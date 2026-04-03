"""Tests for _provider_index_job versioned snapshot handling (Bug #604).

Verifies that when repo_path points to a versioned snapshot (.versioned/ in path),
_provider_index_job indexes the BASE CLONE instead, then creates a new snapshot.

subprocess is imported locally inside _provider_index_job, so it must be patched
at the stdlib level ("subprocess.run"), not at the handlers module level.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch


class TestProviderIndexJobVersionedSnapshot:
    """_provider_index_job must use base clone when given a versioned snapshot path."""

    def _make_versioned_path(self, tmp_path: Path) -> tuple[Path, Path]:
        """Create a minimal versioned snapshot + base clone directory structure."""
        golden_repos_dir = tmp_path / "golden-repos"

        # Base clone: golden-repos/claude-server/
        base_clone = golden_repos_dir / "claude-server"
        base_clone.mkdir(parents=True)
        (base_clone / ".code-indexer").mkdir()
        config = {"embedding_provider": "voyage-ai"}
        (base_clone / ".code-indexer" / "config.json").write_text(json.dumps(config))

        # Versioned snapshot: golden-repos/.versioned/claude-server/v_1772136021/
        versioned = golden_repos_dir / ".versioned" / "claude-server" / "v_1772136021"
        versioned.mkdir(parents=True)
        (versioned / ".code-indexer").mkdir()
        (versioned / ".code-indexer" / "config.json").write_text(json.dumps(config))

        return base_clone, versioned

    def test_uses_base_clone_for_versioned_snapshot_path(self, tmp_path):
        """When repo_path is inside .versioned/, cidx index must run on the base clone."""
        base_clone, versioned = self._make_versioned_path(tmp_path)

        from code_indexer.server.mcp.handlers import _provider_index_job

        completed = MagicMock(returncode=0, stdout="indexed", stderr="")

        with (
            patch("subprocess.run", return_value=completed) as mock_run,
            patch("code_indexer.server.mcp.handlers.get_config_service") as mock_cfg,
            patch("code_indexer.server.mcp.handlers._post_provider_index_snapshot"),
        ):
            mock_cfg.return_value.get_config.return_value = MagicMock(
                cohere_api_key="test-key", voyageai_api_key=None
            )

            result = _provider_index_job(
                repo_path=str(versioned),
                provider_name="cohere",
                clear=False,
                repo_alias="claude-server-global",
            )

        assert result["success"] is True
        # subprocess must be called with cwd=base_clone, NOT cwd=versioned
        mock_run.assert_called_once()
        _, call_kwargs = mock_run.call_args
        assert call_kwargs["cwd"] == str(base_clone), (
            f"Expected cwd={base_clone}, got {call_kwargs['cwd']}"
        )

    def test_does_not_use_base_clone_for_non_versioned_path(self, tmp_path):
        """When repo_path is NOT a versioned snapshot, use it as-is (click-style repos)."""
        golden_repos_dir = tmp_path / "golden-repos"
        repo_dir = golden_repos_dir / "click"
        repo_dir.mkdir(parents=True)
        (repo_dir / ".code-indexer").mkdir()
        config = {"embedding_provider": "voyage-ai"}
        (repo_dir / ".code-indexer" / "config.json").write_text(json.dumps(config))

        from code_indexer.server.mcp.handlers import _provider_index_job

        completed = MagicMock(returncode=0, stdout="indexed", stderr="")

        with (
            patch("subprocess.run", return_value=completed) as mock_run,
            patch("code_indexer.server.mcp.handlers.get_config_service") as mock_cfg,
            patch(
                "code_indexer.server.mcp.handlers._post_provider_index_snapshot"
            ) as mock_snapshot,
        ):
            mock_cfg.return_value.get_config.return_value = MagicMock(
                cohere_api_key="test-key", voyageai_api_key=None
            )

            result = _provider_index_job(
                repo_path=str(repo_dir),
                provider_name="cohere",
                clear=False,
                repo_alias="click-global",
            )

        assert result["success"] is True
        # Must use the exact repo_dir (no base-clone redirect)
        mock_run.assert_called_once()
        _, call_kwargs = mock_run.call_args
        assert call_kwargs["cwd"] == str(repo_dir)
        # No snapshot creation for non-versioned repos
        mock_snapshot.assert_not_called()

    def test_calls_post_snapshot_after_successful_versioned_index(self, tmp_path):
        """After indexing base clone, _post_provider_index_snapshot must be called."""
        base_clone, versioned = self._make_versioned_path(tmp_path)

        from code_indexer.server.mcp.handlers import _provider_index_job

        completed = MagicMock(returncode=0, stdout="indexed", stderr="")

        with (
            patch("subprocess.run", return_value=completed),
            patch("code_indexer.server.mcp.handlers.get_config_service") as mock_cfg,
            patch(
                "code_indexer.server.mcp.handlers._post_provider_index_snapshot"
            ) as mock_snapshot,
        ):
            mock_cfg.return_value.get_config.return_value = MagicMock(
                cohere_api_key="test-key", voyageai_api_key=None
            )

            _provider_index_job(
                repo_path=str(versioned),
                provider_name="cohere",
                clear=False,
                repo_alias="claude-server-global",
            )

        mock_snapshot.assert_called_once_with(
            repo_alias="claude-server-global",
            base_clone_path=str(base_clone),
            old_snapshot_path=str(versioned),
        )

    def test_no_snapshot_on_index_failure(self, tmp_path):
        """If cidx index fails, _post_provider_index_snapshot must NOT be called."""
        base_clone, versioned = self._make_versioned_path(tmp_path)

        from code_indexer.server.mcp.handlers import _provider_index_job

        failed = MagicMock(returncode=1, stdout="", stderr="error")

        with (
            patch("subprocess.run", return_value=failed),
            patch("code_indexer.server.mcp.handlers.get_config_service") as mock_cfg,
            patch(
                "code_indexer.server.mcp.handlers._post_provider_index_snapshot"
            ) as mock_snapshot,
        ):
            mock_cfg.return_value.get_config.return_value = MagicMock(
                cohere_api_key="test-key", voyageai_api_key=None
            )

            result = _provider_index_job(
                repo_path=str(versioned),
                provider_name="cohere",
                clear=False,
                repo_alias="claude-server-global",
            )

        assert result["success"] is False
        mock_snapshot.assert_not_called()

    def test_config_json_mutated_in_base_clone_not_snapshot(self, tmp_path):
        """The config.json that gets mutated must be in the base clone, not the snapshot."""
        base_clone, versioned = self._make_versioned_path(tmp_path)

        from code_indexer.server.mcp.handlers import _provider_index_job

        captured_config_writes: list[str] = []

        original_open = open

        def tracking_open(path, mode="r", *args, **kwargs):
            f = original_open(path, mode, *args, **kwargs)
            if mode == "w" and ".code-indexer/config.json" in str(path):
                captured_config_writes.append(str(path))
            return f

        completed = MagicMock(returncode=0, stdout="indexed", stderr="")

        with (
            patch("subprocess.run", return_value=completed),
            patch("code_indexer.server.mcp.handlers.get_config_service") as mock_cfg,
            patch("code_indexer.server.mcp.handlers._post_provider_index_snapshot"),
            patch("builtins.open", side_effect=tracking_open),
        ):
            mock_cfg.return_value.get_config.return_value = MagicMock(
                cohere_api_key="test-key", voyageai_api_key=None
            )

            _provider_index_job(
                repo_path=str(versioned),
                provider_name="cohere",
                clear=False,
                repo_alias="claude-server-global",
            )

        # Writes must have occurred (config.json was mutated)
        assert captured_config_writes, "Expected config.json writes in base clone"
        # All config writes must be in base_clone, never in versioned snapshot
        for write_path in captured_config_writes:
            assert str(write_path).startswith(str(base_clone)), (
                f"config.json was written outside base clone: {write_path}"
            )
            assert ".versioned" not in write_path, (
                f"config.json was written inside .versioned/: {write_path}"
            )
