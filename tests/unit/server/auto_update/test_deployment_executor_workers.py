"""
Tests for uvicorn workers configuration in auto-update.

Tests for DeploymentExecutor._ensure_workers_config() method that adds
--workers 1 to existing systemd service files during auto-update.
Single worker maintains in-memory cache coherency (HNSW, FTS, OmniCache).
"""

from pathlib import Path
from unittest.mock import patch


from code_indexer.server.auto_update.deployment_executor import DeploymentExecutor


class TestEnsureWorkersConfig:
    """AC4: Tests for _ensure_workers_config method."""

    def test_ensure_workers_config_method_exists(self):
        """AC4: DeploymentExecutor should have _ensure_workers_config method."""
        executor = DeploymentExecutor(repo_path=Path("/tmp"))
        assert hasattr(executor, "_ensure_workers_config")
        assert callable(getattr(executor, "_ensure_workers_config"))

    def test_ensure_workers_returns_true_when_service_not_found(self):
        """AC4: Should return True when service file doesn't exist (not an error)."""
        executor = DeploymentExecutor(
            repo_path=Path("/tmp"),
            service_name="nonexistent-service",
        )

        # Mock Path.exists to return False
        with patch.object(Path, "exists", return_value=False):
            result = executor._ensure_workers_config()

        assert result is True

    def test_ensure_workers_returns_true_when_workers_already_present(self):
        """AC4: Should return True without changes if --workers already configured."""
        executor = DeploymentExecutor(
            repo_path=Path("/tmp"),
            service_name="cidx-server",
        )

        service_content = """[Service]
ExecStart=/usr/bin/python3 -m uvicorn app:app --workers 4
"""

        with patch.object(Path, "exists", return_value=True):
            with patch.object(Path, "read_text", return_value=service_content):
                result = executor._ensure_workers_config()

        assert result is True
