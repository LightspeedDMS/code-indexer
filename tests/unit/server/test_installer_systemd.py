"""
Unit tests for ServerInstaller.create_systemd_service method.

Tests systemd service file generation with OAuth issuer URL and API key configuration.
Also tests that create_server_config writes host=0.0.0.0 to match the ExecStart.
"""

import json
import tempfile
from pathlib import Path

from code_indexer.server.installer import ServerInstaller


class TestServerInstallerSystemd:
    """Test suite for ServerInstaller systemd service creation."""

    def test_create_systemd_service_basic(self):
        """Test creating systemd service file with basic configuration."""
        with tempfile.TemporaryDirectory() as temp_dir:
            # Patch home directory to use temp directory
            test_server_dir = Path(temp_dir) / ".cidx-server"
            test_server_dir.mkdir(parents=True)

            installer = ServerInstaller(base_port=8090)
            installer.server_dir = test_server_dir

            # Create systemd service file
            service_path = installer.create_systemd_service(port=8090)

            # Verify file was created
            assert service_path.exists()
            assert service_path.name == "cidx-server.service"

            # Read and verify content
            content = service_path.read_text()
            assert "[Unit]" in content
            assert "Description=CIDX Multi-User Server with MCP Integration" in content
            assert "[Service]" in content
            assert "ExecStart=" in content
            assert "--port 8090" in content
            assert "[Install]" in content
            assert "WantedBy=multi-user.target" in content

    def test_create_server_config_host_matches_execstart(self) -> None:
        """Root-cause hygiene: create_server_config must write host=0.0.0.0 into config.json.

        The systemd ExecStart is hardcoded to --host 0.0.0.0 so HAProxy on another host
        can reach this node. However, ServerConfig.host defaults to 127.0.0.1, so if the
        installer does not explicitly set host in config.json, a routine DEPLOY that falls
        through to config.json would rewrite ExecStart to --host 127.0.0.1, dropping the
        node off the load balancer.

        The primary fix is in _read_launch_source (DEPLOY+missing now returns None,
        preserving the live unit). This is the secondary root-cause fix: align config.json
        with the ExecStart so they are consistent.

        Asserts that config.json written by create_server_config contains host=0.0.0.0.
        """
        with tempfile.TemporaryDirectory() as temp_dir:
            test_server_dir = Path(temp_dir) / ".cidx-server"
            test_server_dir.mkdir(parents=True)

            installer = ServerInstaller(base_port=8091)
            installer.server_dir = test_server_dir
            installer.config_manager.server_dir = test_server_dir
            installer.config_manager.config_file_path = test_server_dir / "config.json"

            installer.create_server_config(port=8091)

            config_path = test_server_dir / "config.json"
            assert config_path.exists(), "create_server_config must write config.json"

            config = json.loads(config_path.read_text())
            assert config.get("host") == "0.0.0.0", (
                f"config.json must contain host=0.0.0.0 to match the ExecStart "
                f"--host 0.0.0.0 hardcoded in the systemd service file. "
                f"Got host={config.get('host')!r}. Without this alignment, if the "
                f"DEPLOY fallback path ever reads config.json it would rewrite the "
                f"ExecStart to 127.0.0.1, dropping the node off HAProxy."
            )
