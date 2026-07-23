"""Bug #1325 (code-review follow-up): the MCP provider-index background jobs
(_provider_index_job / _provider_temporal_index_job, reachable via
bulk_add_provider_index / manage_provider_indexes) spawn `cidx index`
subprocesses with cwd=<actual_path> via _run_provider_subprocess ->
run_with_popen_progress. That env must never inherit a RELATIVE PYTHONPATH
unchanged from the server process.

Root cause and fix are identical to golden_repo_manager.py /
refresh_scheduler.py: when the server is launched via the documented dev
command (`PYTHONPATH=./src python3 -m uvicorn code_indexer.server.app:app`),
a relative PYTHONPATH entry is inherited unchanged by the child, and because
PYTHONPATH resolution is relative to the CURRENT process's cwd while the
child runs with cwd=<repo path>, the relative entry re-anchors into the repo
directory -- shadowing an installed cidx dependency if the repo has a
colliding src/-layout package.

Fix: _run_provider_subprocess (the single shared call site for BOTH the
semantic and temporal provider-index jobs) routes its env through
build_cidx_subprocess_env() before calling run_with_popen_progress, so
PYTHONPATH is absolutized while the provider API key vars (CO_API_KEY /
VOYAGE_API_KEY) and the #1313 temporal PG bootstrap var are preserved.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

_RELATIVE_PYTHONPATH = "./src"


def _make_repo(tmp_path: Path) -> Path:
    """Create a minimal non-versioned repo directory structure."""
    repo_dir = tmp_path / "golden-repos" / "my-repo"
    repo_dir.mkdir(parents=True)
    (repo_dir / ".code-indexer").mkdir()
    config = {"embedding_provider": "cohere"}
    (repo_dir / ".code-indexer" / "config.json").write_text(json.dumps(config))
    return repo_dir


def _mock_popen_proc():
    mock_proc = MagicMock()
    mock_proc.stdout = iter([])
    mock_proc.stderr.readlines.return_value = []
    mock_proc.returncode = 0
    mock_proc.wait.return_value = None
    mock_proc.poll.return_value = 0
    return mock_proc


@pytest.mark.slow
class TestProviderIndexJobSanitizesPythonPath:
    """_provider_index_job (semantic): the Popen env must have absolutized
    PYTHONPATH while preserving the provider API key."""

    def test_semantic_job_receives_absolutized_pythonpath_and_preserves_api_key(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("PYTHONPATH", _RELATIVE_PYTHONPATH)
        expected_abs = os.path.abspath(_RELATIVE_PYTHONPATH)

        repo_dir = _make_repo(tmp_path)
        mock_proc = _mock_popen_proc()
        captured_kwargs = {}

        from code_indexer.server.mcp.handlers import _provider_index_job

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
                return_value=(5, 3),
            ),
            patch("code_indexer.server.mcp.handlers.get_config_service") as mock_cfg,
            patch("code_indexer.server.mcp.handlers._post_provider_index_snapshot"),
        ):
            mock_ci_config = MagicMock()
            mock_ci_config.cohere_api_key = "my-cohere-key-12345"
            mock_ci_config.voyageai_api_key = None
            mock_config = MagicMock()
            mock_config.claude_integration_config = mock_ci_config
            mock_cfg.return_value.get_config.return_value = mock_config

            result = _provider_index_job(
                repo_path=str(repo_dir),
                provider_name="cohere",
                clear=False,
            )

        assert result["success"] is True
        assert "env" in captured_kwargs, "env must be passed to Popen"
        env = captured_kwargs["env"]
        assert env.get("PYTHONPATH") == expected_abs, (
            f"Bug #1325: expected absolutized PYTHONPATH {expected_abs!r}, "
            f"got {env.get('PYTHONPATH')!r}"
        )
        assert env.get("CO_API_KEY") == "my-cohere-key-12345", (
            "provider API key must survive sanitization"
        )


@pytest.mark.slow
class TestProviderTemporalIndexJobSanitizesPythonPath:
    """_provider_temporal_index_job: the Popen env must have absolutized
    PYTHONPATH while preserving the provider API key AND (in postgres mode)
    the #1313 temporal PG bootstrap var."""

    def test_temporal_job_receives_absolutized_pythonpath_and_preserves_api_key(
        self, monkeypatch, tmp_path
    ):
        monkeypatch.setenv("PYTHONPATH", _RELATIVE_PYTHONPATH)
        expected_abs = os.path.abspath(_RELATIVE_PYTHONPATH)

        repo_dir = _make_repo(tmp_path)
        mock_proc = _mock_popen_proc()
        captured_kwargs = {}

        from code_indexer.server.mcp.handlers import _provider_temporal_index_job
        from code_indexer.server.utils.config_manager import ServerConfig

        def fake_popen(cmd, **kwargs):
            captured_kwargs.update(kwargs)
            return mock_proc

        server_config = ServerConfig(
            server_dir="/opt/cidx-server", storage_mode="sqlite"
        )

        with (
            patch(
                "code_indexer.services.progress_subprocess_runner.subprocess.Popen",
                side_effect=fake_popen,
            ),
            patch(
                "code_indexer.services.progress_subprocess_runner.gather_repo_metrics",
                return_value=(5, 3),
            ),
            patch("code_indexer.server.mcp.handlers.get_config_service") as mock_cfg,
            patch("code_indexer.server.mcp.handlers._post_provider_index_snapshot"),
        ):
            mock_ci_config = MagicMock()
            mock_ci_config.cohere_api_key = "my-cohere-key-67890"
            mock_ci_config.voyageai_api_key = None
            mock_config = MagicMock()
            mock_config.claude_integration_config = mock_ci_config
            # Story #1418: _run_provider_subprocess now makes ONE more
            # get_config_service().get_config() call (for the embedding-
            # stats bootstrap wiring), after the provider-key-resolution
            # call and the temporal PG-bootstrap call -- so the side_effect
            # iterator needs a third entry (same server_config).
            mock_cfg.return_value.get_config.side_effect = [
                mock_config,
                server_config,
                server_config,
            ]

            result = _provider_temporal_index_job(
                repo_path=str(repo_dir),
                provider_name="cohere",
                clear=False,
            )

        assert result["success"] is True
        assert "env" in captured_kwargs, "env must be passed to Popen"
        env = captured_kwargs["env"]
        assert env.get("PYTHONPATH") == expected_abs, (
            f"Bug #1325: expected absolutized PYTHONPATH {expected_abs!r}, "
            f"got {env.get('PYTHONPATH')!r}"
        )
        assert env.get("CO_API_KEY") == "my-cohere-key-67890", (
            "provider API key must survive sanitization"
        )

    def test_temporal_job_preserves_pg_bootstrap_var_in_postgres_mode(
        self, monkeypatch, tmp_path
    ):
        from code_indexer.storage.temporal_metadata_backend_registry import (
            TEMPORAL_PG_BOOTSTRAP_DIR_ENV,
        )

        monkeypatch.setenv("PYTHONPATH", _RELATIVE_PYTHONPATH)
        expected_abs = os.path.abspath(_RELATIVE_PYTHONPATH)

        repo_dir = _make_repo(tmp_path)
        mock_proc = _mock_popen_proc()
        captured_kwargs = {}

        from code_indexer.server.mcp.handlers import _provider_temporal_index_job
        from code_indexer.server.utils.config_manager import ServerConfig

        def fake_popen(cmd, **kwargs):
            captured_kwargs.update(kwargs)
            return mock_proc

        server_config = ServerConfig(
            server_dir="/opt/cidx-server",
            storage_mode="postgres",
            postgres_dsn="postgresql://user:pass@host/db",
        )

        with (
            patch(
                "code_indexer.services.progress_subprocess_runner.subprocess.Popen",
                side_effect=fake_popen,
            ),
            patch(
                "code_indexer.services.progress_subprocess_runner.gather_repo_metrics",
                return_value=(5, 3),
            ),
            patch("code_indexer.server.mcp.handlers.get_config_service") as mock_cfg,
            patch("code_indexer.server.mcp.handlers._post_provider_index_snapshot"),
        ):
            mock_ci_config = MagicMock()
            mock_ci_config.cohere_api_key = "my-cohere-key-99999"
            mock_ci_config.voyageai_api_key = None
            mock_config = MagicMock()
            mock_config.claude_integration_config = mock_ci_config
            # Story #1418: see the sibling test above for why this needs a
            # third entry.
            mock_cfg.return_value.get_config.side_effect = [
                mock_config,
                server_config,
                server_config,
            ]

            result = _provider_temporal_index_job(
                repo_path=str(repo_dir),
                provider_name="cohere",
                clear=False,
            )

        assert result["success"] is True
        env = captured_kwargs["env"]
        assert env.get("PYTHONPATH") == expected_abs
        assert env.get("CO_API_KEY") == "my-cohere-key-99999"
        assert env.get(TEMPORAL_PG_BOOTSTRAP_DIR_ENV) == "/opt/cidx-server", (
            "Bug #1313: the temporal PG bootstrap var must survive sanitization"
        )
