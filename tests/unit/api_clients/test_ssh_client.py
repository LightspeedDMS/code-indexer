"""
Unit tests for SSHAPIClient - SSH Key Management API Client.

Story #656: Advanced Operations Parity - SSH Key Management.
Following TDD methodology - tests written before implementation.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


class TestSSHAPIClientImport:
    """Test that SSHAPIClient can be imported."""

    def test_ssh_api_client_can_be_imported(self):
        """Test SSHAPIClient class is importable."""
        from code_indexer.api_clients.ssh_client import SSHAPIClient

        assert SSHAPIClient is not None

    def test_ssh_api_client_inherits_from_base(self):
        """Test SSHAPIClient inherits from CIDXRemoteAPIClient."""
        from code_indexer.api_clients.ssh_client import SSHAPIClient
        from code_indexer.api_clients.base_client import CIDXRemoteAPIClient

        assert issubclass(SSHAPIClient, CIDXRemoteAPIClient)


class TestSSHAPIClientInitialization:
    """Test SSHAPIClient initialization."""

    def test_ssh_api_client_initialization(self):
        """Test SSHAPIClient can be initialized with server_url and credentials."""
        from code_indexer.api_clients.ssh_client import SSHAPIClient

        credentials = {"username": "testuser", "password": "testpass"}
        client = SSHAPIClient(
            server_url="https://test-server.com",
            credentials=credentials,
        )

        assert client.server_url == "https://test-server.com"
        assert client.credentials == credentials

    def test_ssh_api_client_initialization_with_project_root(self, tmp_path):
        """Test SSHAPIClient initialization with project_root."""
        from code_indexer.api_clients.ssh_client import SSHAPIClient

        credentials = {"username": "testuser", "password": "testpass"}
        client = SSHAPIClient(
            server_url="https://test-server.com",
            credentials=credentials,
            project_root=tmp_path,
        )

        assert client.project_root == tmp_path


class TestSSHAPIClientCreateMethod:
    """Tests for SSH key create method."""

    @pytest.fixture
    def ssh_client(self):
        """Create an SSHAPIClient for testing."""
        from code_indexer.api_clients.ssh_client import SSHAPIClient

        credentials = {"username": "testuser", "password": "testpass"}
        return SSHAPIClient(
            server_url="https://test-server.com",
            credentials=credentials,
        )

    @pytest.mark.asyncio
    async def test_create_method_exists(self, ssh_client):
        """Test that create method exists."""
        assert hasattr(ssh_client, "create_key")
        assert callable(ssh_client.create_key)

    @pytest.mark.asyncio
    async def test_create_calls_correct_endpoint(self, ssh_client):
        """Test create calls the correct REST endpoint."""
        with patch.object(
            ssh_client, "_authenticated_request", new_callable=AsyncMock
        ) as mock_request:
            mock_response = MagicMock()
            mock_response.status_code = 201
            mock_response.json.return_value = {
                "name": "my-key",
                "key_type": "ed25519",
                "public_key": "ssh-ed25519 AAAA...",
                "fingerprint": "SHA256:...",
                "created_at": "2025-01-20T00:00:00Z",
            }
            mock_request.return_value = mock_response

            _ = await ssh_client.create_key(
                name="my-key",
                email="test@example.com",
                key_type="ed25519",
                description="Test key",
            )

            mock_request.assert_called_once()
            call_args = mock_request.call_args
            assert call_args[0][0] == "POST"
            assert "/api/v1/ssh/keys" in call_args[0][1]
            assert call_args[1]["json"]["name"] == "my-key"
            assert call_args[1]["json"]["email"] == "test@example.com"
            assert call_args[1]["json"]["key_type"] == "ed25519"

    @pytest.mark.asyncio
    async def test_create_returns_key_data(self, ssh_client):
        """Test create returns the created key data."""
        with patch.object(
            ssh_client, "_authenticated_request", new_callable=AsyncMock
        ) as mock_request:
            mock_response = MagicMock()
            mock_response.status_code = 201
            mock_response.json.return_value = {
                "name": "my-key",
                "key_type": "ed25519",
                "public_key": "ssh-ed25519 AAAA...",
                "fingerprint": "SHA256:abc123",
                "created_at": "2025-01-20T00:00:00Z",
            }
            mock_request.return_value = mock_response

            result = await ssh_client.create_key(
                name="my-key",
                email="test@example.com",
            )

            assert result["name"] == "my-key"
            assert "public_key" in result
            assert "fingerprint" in result


class TestSSHAPIClientListMethod:
    """Tests for SSH key list method."""

    @pytest.fixture
    def ssh_client(self):
        """Create an SSHAPIClient for testing."""
        from code_indexer.api_clients.ssh_client import SSHAPIClient

        credentials = {"username": "testuser", "password": "testpass"}
        return SSHAPIClient(
            server_url="https://test-server.com",
            credentials=credentials,
        )

    @pytest.mark.asyncio
    async def test_list_method_exists(self, ssh_client):
        """Test that list method exists."""
        assert hasattr(ssh_client, "list_keys")
        assert callable(ssh_client.list_keys)

    @pytest.mark.asyncio
    async def test_list_calls_correct_endpoint(self, ssh_client):
        """Test list calls the correct REST endpoint."""
        with patch.object(
            ssh_client, "_authenticated_request", new_callable=AsyncMock
        ) as mock_request:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "keys": [
                    {
                        "name": "key1",
                        "key_type": "ed25519",
                        "fingerprint": "SHA256:abc",
                        "created_at": "2025-01-20T00:00:00Z",
                    },
                    {
                        "name": "key2",
                        "key_type": "rsa",
                        "fingerprint": "SHA256:def",
                        "created_at": "2025-01-19T00:00:00Z",
                    },
                ]
            }
            mock_request.return_value = mock_response

            _ = await ssh_client.list_keys()

            mock_request.assert_called_once()
            call_args = mock_request.call_args
            assert call_args[0][0] == "GET"
            assert "/api/v1/ssh/keys" in call_args[0][1]

    @pytest.mark.asyncio
    async def test_list_returns_keys_array(self, ssh_client):
        """Test list returns an array of keys."""
        with patch.object(
            ssh_client, "_authenticated_request", new_callable=AsyncMock
        ) as mock_request:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "keys": [
                    {"name": "key1", "key_type": "ed25519"},
                    {"name": "key2", "key_type": "rsa"},
                ]
            }
            mock_request.return_value = mock_response

            result = await ssh_client.list_keys()

            assert "keys" in result
            assert len(result["keys"]) == 2


class TestSSHAPIClientDeleteMethod:
    """Tests for SSH key delete method."""

    @pytest.fixture
    def ssh_client(self):
        """Create an SSHAPIClient for testing."""
        from code_indexer.api_clients.ssh_client import SSHAPIClient

        credentials = {"username": "testuser", "password": "testpass"}
        return SSHAPIClient(
            server_url="https://test-server.com",
            credentials=credentials,
        )

    @pytest.mark.asyncio
    async def test_delete_method_exists(self, ssh_client):
        """Test that delete method exists."""
        assert hasattr(ssh_client, "delete_key")
        assert callable(ssh_client.delete_key)

    @pytest.mark.asyncio
    async def test_delete_calls_correct_endpoint(self, ssh_client):
        """Test delete calls the correct REST endpoint."""
        with patch.object(
            ssh_client, "_authenticated_request", new_callable=AsyncMock
        ) as mock_request:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "deleted": True,
                "name": "my-key",
            }
            mock_request.return_value = mock_response

            await ssh_client.delete_key("my-key")

            mock_request.assert_called_once()
            call_args = mock_request.call_args
            assert call_args[0][0] == "DELETE"
            assert "/api/v1/ssh/keys/my-key" in call_args[0][1]


class TestSSHAPIClientShowPublicMethod:
    """Tests for SSH key show-public method."""

    @pytest.fixture
    def ssh_client(self):
        """Create an SSHAPIClient for testing."""
        from code_indexer.api_clients.ssh_client import SSHAPIClient

        credentials = {"username": "testuser", "password": "testpass"}
        return SSHAPIClient(
            server_url="https://test-server.com",
            credentials=credentials,
        )

    @pytest.mark.asyncio
    async def test_show_public_method_exists(self, ssh_client):
        """Test that show_public method exists."""
        assert hasattr(ssh_client, "show_public_key")
        assert callable(ssh_client.show_public_key)

    @pytest.mark.asyncio
    async def test_show_public_calls_correct_endpoint(self, ssh_client):
        """Test show_public calls the correct REST endpoint."""
        with patch.object(
            ssh_client, "_authenticated_request", new_callable=AsyncMock
        ) as mock_request:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "name": "my-key",
                "public_key": "ssh-ed25519 AAAA... test@example.com",
                "key_type": "ed25519",
                "fingerprint": "SHA256:abc123",
            }
            mock_request.return_value = mock_response

            _ = await ssh_client.show_public_key("my-key")

            mock_request.assert_called_once()
            call_args = mock_request.call_args
            assert call_args[0][0] == "GET"
            assert "/api/v1/ssh/keys/my-key" in call_args[0][1]

    @pytest.mark.asyncio
    async def test_show_public_returns_public_key(self, ssh_client):
        """Test show_public returns the public key."""
        with patch.object(
            ssh_client, "_authenticated_request", new_callable=AsyncMock
        ) as mock_request:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "name": "my-key",
                "public_key": "ssh-ed25519 AAAA... test@example.com",
            }
            mock_request.return_value = mock_response

            result = await ssh_client.show_public_key("my-key")

            assert "public_key" in result
            assert result["public_key"].startswith("ssh-ed25519")


class TestSSHAPIClientAssignMethod:
    """Tests for SSH key assign method."""

    @pytest.fixture
    def ssh_client(self):
        """Create an SSHAPIClient for testing."""
        from code_indexer.api_clients.ssh_client import SSHAPIClient

        credentials = {"username": "testuser", "password": "testpass"}
        return SSHAPIClient(
            server_url="https://test-server.com",
            credentials=credentials,
        )

    @pytest.mark.asyncio
    async def test_assign_method_exists(self, ssh_client):
        """Test that assign method exists."""
        assert hasattr(ssh_client, "assign_key")
        assert callable(ssh_client.assign_key)

    @pytest.mark.asyncio
    async def test_assign_calls_correct_endpoint(self, ssh_client):
        """Test assign calls the correct REST endpoint."""
        with patch.object(
            ssh_client, "_authenticated_request", new_callable=AsyncMock
        ) as mock_request:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "assigned": True,
                "key_name": "my-key",
                "hostname": "github.com",
            }
            mock_request.return_value = mock_response

            await ssh_client.assign_key("my-key", "github.com")

            mock_request.assert_called_once()
            call_args = mock_request.call_args
            assert call_args[0][0] == "POST"
            assert "/api/v1/ssh/keys/my-key/assign" in call_args[0][1]
            assert call_args[1]["json"]["hostname"] == "github.com"

    @pytest.mark.asyncio
    async def test_assign_with_force_option(self, ssh_client):
        """Test assign with force option."""
        with patch.object(
            ssh_client, "_authenticated_request", new_callable=AsyncMock
        ) as mock_request:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "assigned": True,
                "key_name": "my-key",
                "hostname": "github.com",
                "replaced_existing": True,
            }
            mock_request.return_value = mock_response

            await ssh_client.assign_key("my-key", "github.com", force=True)

            mock_request.assert_called_once()
            call_args = mock_request.call_args
            assert call_args[1]["json"]["force"] is True


class TestSSHAPIClientErrorHandling:
    """Tests for SSH API client error handling."""

    @pytest.fixture
    def ssh_client(self):
        """Create an SSHAPIClient for testing."""
        from code_indexer.api_clients.ssh_client import SSHAPIClient

        credentials = {"username": "testuser", "password": "testpass"}
        return SSHAPIClient(
            server_url="https://test-server.com",
            credentials=credentials,
        )

    @pytest.mark.asyncio
    async def test_create_handles_conflict_error(self, ssh_client):
        """Test create handles 409 conflict (key already exists)."""
        from code_indexer.api_clients.base_client import APIClientError

        with patch.object(
            ssh_client, "_authenticated_request", new_callable=AsyncMock
        ) as mock_request:
            mock_response = MagicMock()
            mock_response.status_code = 409
            mock_response.json.return_value = {"detail": "Key 'my-key' already exists"}
            mock_request.return_value = mock_response

            with pytest.raises(APIClientError) as exc_info:
                await ssh_client.create_key(
                    name="my-key",
                    email="test@example.com",
                )

            assert "already exists" in str(exc_info.value)

    @pytest.mark.asyncio
    async def test_delete_handles_not_found_error(self, ssh_client):
        """Test delete handles 404 not found."""
        from code_indexer.api_clients.base_client import APIClientError

        with patch.object(
            ssh_client, "_authenticated_request", new_callable=AsyncMock
        ) as mock_request:
            mock_response = MagicMock()
            mock_response.status_code = 404
            mock_response.json.return_value = {"detail": "Key 'nonexistent' not found"}
            mock_request.return_value = mock_response

            with pytest.raises(APIClientError) as exc_info:
                await ssh_client.delete_key("nonexistent")

            assert "not found" in str(exc_info.value).lower()
