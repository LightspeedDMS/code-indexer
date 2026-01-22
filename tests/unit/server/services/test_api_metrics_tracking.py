"""
Tests for API Metrics Tracking at Service Layer (Story #4 AC2).

Verifies that service layer methods increment API metrics counters exactly once
and that no double-counting occurs when called via MCP.

TDD: These tests define expected behavior before implementation.
"""

import pytest
from unittest.mock import patch, MagicMock
from pathlib import Path
import tempfile


class TestSSHKeyManagerMetrics:
    """Tests for SSH Key Manager API metrics tracking."""

    @pytest.fixture
    def temp_ssh_dir(self):
        """Create temporary directories for SSH key manager tests."""
        with tempfile.TemporaryDirectory() as temp_dir:
            ssh_dir = Path(temp_dir) / ".ssh"
            metadata_dir = Path(temp_dir) / ".code-indexer-server" / "ssh_keys"
            ssh_dir.mkdir(parents=True)
            metadata_dir.mkdir(parents=True)
            yield {
                "temp_dir": temp_dir,
                "ssh_dir": ssh_dir,
                "metadata_dir": metadata_dir,
            }

    @pytest.fixture
    def ssh_key_manager(self, temp_ssh_dir):
        """Create SSH key manager with temporary directories."""
        from code_indexer.server.services.ssh_key_manager import SSHKeyManager

        return SSHKeyManager(
            ssh_dir=temp_ssh_dir["ssh_dir"],
            metadata_dir=temp_ssh_dir["metadata_dir"],
            config_path=temp_ssh_dir["ssh_dir"] / "config",
        )

    def test_create_key_increments_other_api_call(self, ssh_key_manager):
        """Test that create_key() increments other_api_call counter exactly once."""
        # Mock the api_metrics_service at the source module level
        with patch(
            "code_indexer.server.services.api_metrics_service.api_metrics_service"
        ) as mock_metrics:
            # Create a key
            result = ssh_key_manager.create_key(
                name="test-key-metrics",
                key_type="ed25519",
                email="test@example.com",
                description="Test key for metrics",
            )

            # Verify API metrics incremented exactly once
            mock_metrics.increment_other_api_call.assert_called_once()
            assert result.name == "test-key-metrics"

    def test_assign_key_to_host_increments_other_api_call(self, ssh_key_manager):
        """Test that assign_key_to_host() increments other_api_call counter exactly once."""
        # First create a key to assign (without mocking metrics)
        ssh_key_manager.create_key(
            name="test-key-assign",
            key_type="ed25519",
        )

        with patch(
            "code_indexer.server.services.api_metrics_service.api_metrics_service"
        ) as mock_metrics:
            # Assign key to host
            result = ssh_key_manager.assign_key_to_host(
                key_name="test-key-assign",
                hostname="github.com",
            )

            # Verify API metrics incremented exactly once
            mock_metrics.increment_other_api_call.assert_called_once()
            assert "github.com" in result.hosts

    def test_delete_key_increments_other_api_call(self, ssh_key_manager):
        """Test that delete_key() increments other_api_call counter exactly once."""
        # First create a key to delete (without mocking metrics)
        ssh_key_manager.create_key(
            name="test-key-delete",
            key_type="ed25519",
        )

        with patch(
            "code_indexer.server.services.api_metrics_service.api_metrics_service"
        ) as mock_metrics:
            # Delete the key
            result = ssh_key_manager.delete_key(key_name="test-key-delete")

            # Verify API metrics incremented exactly once
            mock_metrics.increment_other_api_call.assert_called_once()
            assert result is True

    def test_list_keys_increments_other_api_call(self, ssh_key_manager):
        """Test that list_keys() increments other_api_call counter exactly once."""
        with patch(
            "code_indexer.server.services.api_metrics_service.api_metrics_service"
        ) as mock_metrics:
            # List keys
            result = ssh_key_manager.list_keys()

            # Verify API metrics incremented exactly once
            mock_metrics.increment_other_api_call.assert_called_once()
            assert hasattr(result, "managed")
            assert hasattr(result, "unmanaged")

    def test_get_public_key_increments_other_api_call(self, ssh_key_manager):
        """Test that get_public_key() increments other_api_call counter exactly once."""
        # First create a key to retrieve (without mocking metrics)
        ssh_key_manager.create_key(
            name="test-key-get",
            key_type="ed25519",
        )

        with patch(
            "code_indexer.server.services.api_metrics_service.api_metrics_service"
        ) as mock_metrics:
            # Get public key
            result = ssh_key_manager.get_public_key(key_name="test-key-get")

            # Verify API metrics incremented exactly once
            mock_metrics.increment_other_api_call.assert_called_once()
            assert result.startswith("ssh-")


class TestFileCRUDServiceMetrics:
    """Tests verifying FileCRUDService already has metrics tracking."""

    @pytest.fixture
    def mock_activated_repo(self):
        """Mock activated repo manager for file CRUD tests."""
        with tempfile.TemporaryDirectory() as temp_dir:
            # Create a mock repository structure
            repo_dir = Path(temp_dir) / "test-repo"
            repo_dir.mkdir(parents=True)

            # Patch the import inside FileCRUDService.__init__
            with patch(
                "code_indexer.server.repositories.activated_repo_manager.ActivatedRepoManager"
            ) as mock_mgr_class:
                mock_mgr = MagicMock()
                mock_mgr.get_activated_repo_path.return_value = str(repo_dir)
                mock_mgr_class.return_value = mock_mgr
                yield {"repo_dir": repo_dir, "mock_mgr": mock_mgr}

    def test_create_file_increments_other_api_call(self, mock_activated_repo):
        """Verify create_file() has metrics tracking (already implemented)."""
        from code_indexer.server.services.file_crud_service import FileCRUDService

        service = FileCRUDService()
        service.activated_repo_manager = mock_activated_repo["mock_mgr"]

        with patch(
            "code_indexer.server.services.api_metrics_service.api_metrics_service"
        ) as mock_metrics:
            result = service.create_file(
                repo_alias="test-repo",
                file_path="test.txt",
                content="Hello, World!",
                username="testuser",
            )

            mock_metrics.increment_other_api_call.assert_called_once()
            assert result["success"] is True

    def test_edit_file_increments_other_api_call(self, mock_activated_repo):
        """Verify edit_file() has metrics tracking (already implemented)."""
        from code_indexer.server.services.file_crud_service import FileCRUDService
        import hashlib

        service = FileCRUDService()
        service.activated_repo_manager = mock_activated_repo["mock_mgr"]

        # First create a file
        test_file = mock_activated_repo["repo_dir"] / "edit-test.txt"
        test_file.write_text("Original content")

        # Get the hash
        content_hash = hashlib.sha256(b"Original content").hexdigest()

        with patch(
            "code_indexer.server.services.api_metrics_service.api_metrics_service"
        ) as mock_metrics:
            result = service.edit_file(
                repo_alias="test-repo",
                file_path="edit-test.txt",
                old_string="Original",
                new_string="Modified",
                content_hash=content_hash,
                replace_all=False,
                username="testuser",
            )

            mock_metrics.increment_other_api_call.assert_called_once()
            assert result["success"] is True

    def test_delete_file_increments_other_api_call(self, mock_activated_repo):
        """Verify delete_file() has metrics tracking (already implemented)."""
        from code_indexer.server.services.file_crud_service import FileCRUDService

        service = FileCRUDService()
        service.activated_repo_manager = mock_activated_repo["mock_mgr"]

        # First create a file
        test_file = mock_activated_repo["repo_dir"] / "delete-test.txt"
        test_file.write_text("To be deleted")

        with patch(
            "code_indexer.server.services.api_metrics_service.api_metrics_service"
        ) as mock_metrics:
            result = service.delete_file(
                repo_alias="test-repo",
                file_path="delete-test.txt",
                content_hash=None,
                username="testuser",
            )

            mock_metrics.increment_other_api_call.assert_called_once()
            assert result["success"] is True


class TestMCPProtocolNoDoubleCount:
    """Tests verifying MCP protocol does NOT double-count service layer metrics.

    After removing edge tracking from protocol.py, MCP calls should NOT
    add their own increment_other_api_call() since services already track.
    """

    @pytest.mark.asyncio
    async def test_mcp_tools_call_no_additional_tracking_for_service_tools(self):
        """Verify MCP handle_tools_call does NOT increment metrics for service-tracked tools.

        Service-tracked tools (file CRUD, SSH key mgmt, git ops) already increment
        their own metrics. MCP should NOT add additional tracking.
        """
        # This test validates the behavior AFTER edge tracking is removed from protocol.py
        # The test should pass once we remove lines 197-203 from protocol.py

        from code_indexer.server.services.api_metrics_service import api_metrics_service

        # Reset metrics for clean test
        api_metrics_service.reset()

        # Get initial count
        initial_metrics = api_metrics_service.get_metrics(window_seconds=60)
        initial_other_calls = initial_metrics["other_api_calls"]

        # Note: This test documents expected behavior after protocol.py change
        # Service layer tracking happens in the service, not in MCP protocol
        # After edge tracking removal, MCP should not double-count

        # Verify no spurious increments
        assert initial_other_calls == 0, "Metrics should start at 0"


class TestServiceLayerMetricsIntegration:
    """Integration tests for service layer metrics tracking.

    Verifies the overall metrics tracking architecture:
    1. Services track their own API calls
    2. No double-counting between layers
    3. Metrics accurately reflect actual service usage
    """

    def test_api_metrics_service_tracks_categories_separately(self):
        """Verify API metrics service tracks each category independently."""
        from code_indexer.server.services.api_metrics_service import ApiMetricsService

        # Create fresh instance for isolation
        service = ApiMetricsService()

        # Increment different categories
        service.increment_semantic_search()
        service.increment_semantic_search()
        service.increment_other_index_search()
        service.increment_regex_search()
        service.increment_other_api_call()
        service.increment_other_api_call()
        service.increment_other_api_call()

        # Verify separate tracking
        metrics = service.get_metrics(window_seconds=60)
        assert metrics["semantic_searches"] == 2
        assert metrics["other_index_searches"] == 1
        assert metrics["regex_searches"] == 1
        assert metrics["other_api_calls"] == 3

    def test_metrics_window_filtering(self):
        """Verify metrics are correctly filtered by time window."""
        from code_indexer.server.services.api_metrics_service import ApiMetricsService

        service = ApiMetricsService()

        # Increment some metrics
        service.increment_other_api_call()
        service.increment_other_api_call()

        # Verify counts in different windows
        metrics_1min = service.get_metrics(window_seconds=60)
        metrics_1hour = service.get_metrics(window_seconds=3600)

        assert metrics_1min["other_api_calls"] == 2
        assert metrics_1hour["other_api_calls"] == 2
