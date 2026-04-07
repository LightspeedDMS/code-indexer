"""Tests for _provider_index_job versioned snapshot handling (Bug #604).

Verifies that when repo_path points to a versioned snapshot (.versioned/ in path),
_provider_index_job indexes the BASE CLONE instead, then creates a new snapshot.

Story #613: subprocess.run replaced with run_with_popen_progress (uses Popen internally).
Mocks must target code_indexer.services.progress_subprocess_runner.subprocess.Popen
and gather_repo_metrics, NOT subprocess.run.
"""

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest


def _mock_popen_proc():
    """Return a mock Popen process that exits successfully with no output."""
    mock_proc = MagicMock()
    mock_proc.stdout = iter([])
    mock_proc.stderr.readlines.return_value = []
    mock_proc.returncode = 0
    mock_proc.wait.return_value = None
    mock_proc.poll.return_value = 0
    return mock_proc


@pytest.mark.slow
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

        mock_proc = _mock_popen_proc()
        captured_kwargs = {}

        def fake_popen(cmd, **kwargs):
            captured_kwargs.update(kwargs)
            return mock_proc

        with (
            patch(
                "code_indexer.services.progress_subprocess_runner.subprocess.Popen",
                side_effect=fake_popen,
            ),
            patch(
                "code_indexer.services.progress_subprocess_runner.gather_repo_metrics",
                return_value=(10, 5),
            ),
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
        # Popen must be called with cwd=base_clone, NOT cwd=versioned
        assert "cwd" in captured_kwargs, "cwd must be passed to Popen"
        assert captured_kwargs["cwd"] == str(base_clone), (
            f"Expected cwd={base_clone}, got {captured_kwargs['cwd']}"
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

        mock_proc = _mock_popen_proc()
        captured_kwargs = {}

        def fake_popen(cmd, **kwargs):
            captured_kwargs.update(kwargs)
            return mock_proc

        with (
            patch(
                "code_indexer.services.progress_subprocess_runner.subprocess.Popen",
                side_effect=fake_popen,
            ),
            patch(
                "code_indexer.services.progress_subprocess_runner.gather_repo_metrics",
                return_value=(10, 5),
            ),
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
        assert captured_kwargs["cwd"] == str(repo_dir)
        # No snapshot creation for non-versioned repos
        mock_snapshot.assert_not_called()

    def test_calls_post_snapshot_after_successful_versioned_index(self, tmp_path):
        """After indexing base clone, _post_provider_index_snapshot must be called."""
        base_clone, versioned = self._make_versioned_path(tmp_path)

        from code_indexer.server.mcp.handlers import _provider_index_job

        mock_proc = _mock_popen_proc()

        with (
            patch(
                "code_indexer.services.progress_subprocess_runner.subprocess.Popen",
                return_value=mock_proc,
            ),
            patch(
                "code_indexer.services.progress_subprocess_runner.gather_repo_metrics",
                return_value=(10, 5),
            ),
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

        # Popen process that exits with returncode=1 (failure)
        mock_proc = MagicMock()
        mock_proc.stdout = iter([])
        mock_proc.stderr.readlines.return_value = ["error output"]
        mock_proc.returncode = 1
        mock_proc.wait.return_value = None
        mock_proc.poll.return_value = 1

        with (
            patch(
                "code_indexer.services.progress_subprocess_runner.subprocess.Popen",
                return_value=mock_proc,
            ),
            patch(
                "code_indexer.services.progress_subprocess_runner.gather_repo_metrics",
                return_value=(10, 5),
            ),
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

    def test_config_json_not_written_during_provider_index_job(self, tmp_path):
        """Story #620: _provider_index_job must NOT write config.json at all (mutation pattern deleted)."""
        base_clone, versioned = self._make_versioned_path(tmp_path)

        from code_indexer.server.mcp.handlers import _provider_index_job

        captured_config_writes: list[str] = []

        original_open = open

        def tracking_open(path, mode="r", *args, **kwargs):
            f = original_open(path, mode, *args, **kwargs)
            if mode == "w" and ".code-indexer/config.json" in str(path):
                captured_config_writes.append(str(path))
            return f

        mock_proc = _mock_popen_proc()

        with (
            patch(
                "code_indexer.services.progress_subprocess_runner.subprocess.Popen",
                return_value=mock_proc,
            ),
            patch(
                "code_indexer.services.progress_subprocess_runner.gather_repo_metrics",
                return_value=(10, 5),
            ),
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

        # No config.json writes must occur — mutation pattern fully deleted (Story #620)
        assert not captured_config_writes, (
            f"Expected zero config.json writes but got: {captured_config_writes}"
        )
