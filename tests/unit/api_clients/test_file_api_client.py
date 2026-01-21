"""
Unit tests for FileAPIClient - File CRUD Operations API Client.

Story #738: CLI remote mode file management operations.
Following TDD methodology - tests written before implementation.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


class TestFileAPIClientImport:
    """Test that FileAPIClient can be imported."""

    def test_file_api_client_can_be_imported(self):
        """Test FileAPIClient class is importable."""
        from code_indexer.api_clients.file_client import FileAPIClient

        assert FileAPIClient is not None

    def test_file_api_client_inherits_from_base(self):
        """Test FileAPIClient inherits from CIDXRemoteAPIClient."""
        from code_indexer.api_clients.file_client import FileAPIClient
        from code_indexer.api_clients.base_client import CIDXRemoteAPIClient

        assert issubclass(FileAPIClient, CIDXRemoteAPIClient)


class TestFileAPIClientInitialization:
    """Test FileAPIClient initialization."""

    def test_file_api_client_initialization(self):
        """Test FileAPIClient can be initialized with server_url and credentials."""
        from code_indexer.api_clients.file_client import FileAPIClient

        credentials = {"username": "testuser", "password": "testpass"}
        client = FileAPIClient(
            server_url="https://test-server.com",
            credentials=credentials,
        )

        assert client.server_url == "https://test-server.com"
        assert client.credentials == credentials

    def test_file_api_client_initialization_with_project_root(self, tmp_path):
        """Test FileAPIClient initialization with project_root."""
        from code_indexer.api_clients.file_client import FileAPIClient

        credentials = {"username": "testuser", "password": "testpass"}
        client = FileAPIClient(
            server_url="https://test-server.com",
            credentials=credentials,
            project_root=tmp_path,
        )

        assert client.project_root == tmp_path


class TestFileAPIClientCreateMethod:
    """Tests for file create method."""

    @pytest.fixture
    def file_client(self):
        """Create a FileAPIClient for testing."""
        from code_indexer.api_clients.file_client import FileAPIClient

        credentials = {"username": "testuser", "password": "testpass"}
        return FileAPIClient(
            server_url="https://test-server.com",
            credentials=credentials,
        )

    @pytest.mark.asyncio
    async def test_create_file_method_exists(self, file_client):
        """Test that create_file method exists."""
        assert hasattr(file_client, "create_file")
        assert callable(file_client.create_file)

    @pytest.mark.asyncio
    async def test_create_file_calls_correct_endpoint(self, file_client):
        """Test create_file calls the correct REST endpoint."""
        with patch.object(
            file_client, "_authenticated_request", new_callable=AsyncMock
        ) as mock_request:
            mock_response = MagicMock()
            mock_response.status_code = 201
            mock_response.json.return_value = {
                "success": True,
                "file_path": "src/new_file.py",
                "content_hash": "abc123def456",
                "size_bytes": 100,
                "created_at": "2025-01-19T12:00:00Z",
            }
            mock_request.return_value = mock_response

            await file_client.create_file(
                "test-repo", "src/new_file.py", "print('hello')"
            )

            mock_request.assert_called_once()
            call_args = mock_request.call_args
            assert call_args[0][0] == "POST"
            assert "/api/v1/repos/test-repo/files" in call_args[0][1]
            assert call_args[1]["json"]["file_path"] == "src/new_file.py"
            assert call_args[1]["json"]["content"] == "print('hello')"

    @pytest.mark.asyncio
    async def test_create_file_returns_response_dict(self, file_client):
        """Test create_file returns response as dictionary."""
        with patch.object(
            file_client, "_authenticated_request", new_callable=AsyncMock
        ) as mock_request:
            mock_response = MagicMock()
            mock_response.status_code = 201
            mock_response.json.return_value = {
                "success": True,
                "file_path": "src/new_file.py",
                "content_hash": "abc123",
                "size_bytes": 100,
                "created_at": "2025-01-19T12:00:00Z",
            }
            mock_request.return_value = mock_response

            result = await file_client.create_file(
                "test-repo", "src/new_file.py", "content"
            )

            assert isinstance(result, dict)
            assert result["success"] is True
            assert result["file_path"] == "src/new_file.py"
            assert "content_hash" in result


class TestFileAPIClientEditMethod:
    """Tests for file edit method."""

    @pytest.fixture
    def file_client(self):
        """Create a FileAPIClient for testing."""
        from code_indexer.api_clients.file_client import FileAPIClient

        credentials = {"username": "testuser", "password": "testpass"}
        return FileAPIClient(
            server_url="https://test-server.com",
            credentials=credentials,
        )

    @pytest.mark.asyncio
    async def test_edit_file_method_exists(self, file_client):
        """Test that edit_file method exists."""
        assert hasattr(file_client, "edit_file")
        assert callable(file_client.edit_file)

    @pytest.mark.asyncio
    async def test_edit_file_calls_correct_endpoint(self, file_client):
        """Test edit_file calls the correct REST endpoint with URL encoding."""
        with patch.object(
            file_client, "_authenticated_request", new_callable=AsyncMock
        ) as mock_request:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "success": True,
                "file_path": "src/app.py",
                "content_hash": "newhash123",
                "modified_at": "2025-01-19T12:00:00Z",
                "changes_made": 1,
            }
            mock_request.return_value = mock_response

            await file_client.edit_file(
                "test-repo",
                "src/app.py",
                old_string="old_value",
                new_string="new_value",
                content_hash="oldhash123",
            )

            mock_request.assert_called_once()
            call_args = mock_request.call_args
            assert call_args[0][0] == "PATCH"
            # File path should be URL-encoded in the URL
            assert "/api/v1/repos/test-repo/files/" in call_args[0][1]
            assert call_args[1]["json"]["old_string"] == "old_value"
            assert call_args[1]["json"]["new_string"] == "new_value"
            assert call_args[1]["json"]["content_hash"] == "oldhash123"

    @pytest.mark.asyncio
    async def test_edit_file_with_replace_all(self, file_client):
        """Test edit_file with replace_all flag."""
        with patch.object(
            file_client, "_authenticated_request", new_callable=AsyncMock
        ) as mock_request:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "success": True,
                "file_path": "src/app.py",
                "content_hash": "newhash",
                "modified_at": "2025-01-19T12:00:00Z",
                "changes_made": 3,
            }
            mock_request.return_value = mock_response

            await file_client.edit_file(
                "test-repo",
                "src/app.py",
                old_string="foo",
                new_string="bar",
                content_hash="hash123",
                replace_all=True,
            )

            mock_request.assert_called_once()
            call_args = mock_request.call_args
            assert call_args[1]["json"]["replace_all"] is True

    @pytest.mark.asyncio
    async def test_edit_file_without_content_hash(self, file_client):
        """Test edit_file without content_hash (optional parameter)."""
        with patch.object(
            file_client, "_authenticated_request", new_callable=AsyncMock
        ) as mock_request:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "success": True,
                "file_path": "src/app.py",
                "content_hash": "newhash",
                "modified_at": "2025-01-19T12:00:00Z",
                "changes_made": 1,
            }
            mock_request.return_value = mock_response

            await file_client.edit_file(
                "test-repo",
                "src/app.py",
                old_string="foo",
                new_string="bar",
            )

            mock_request.assert_called_once()
            call_args = mock_request.call_args
            # content_hash should not be in the JSON when not provided
            json_data = call_args[1]["json"]
            assert (
                "content_hash" not in json_data or json_data.get("content_hash") is None
            )

    @pytest.mark.asyncio
    async def test_edit_file_url_encodes_path(self, file_client):
        """Test edit_file URL-encodes file paths with special characters."""
        with patch.object(
            file_client, "_authenticated_request", new_callable=AsyncMock
        ) as mock_request:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "success": True,
                "file_path": "src/my file.py",
                "content_hash": "hash",
                "modified_at": "2025-01-19T12:00:00Z",
                "changes_made": 1,
            }
            mock_request.return_value = mock_response

            await file_client.edit_file(
                "test-repo",
                "src/my file.py",
                old_string="old",
                new_string="new",
            )

            mock_request.assert_called_once()
            call_args = mock_request.call_args
            # Path should be URL-encoded (space becomes %20)
            endpoint = call_args[0][1]
            assert "src%2Fmy%20file.py" in endpoint or "my%20file.py" in endpoint


class TestFileAPIClientDeleteMethod:
    """Tests for file delete method."""

    @pytest.fixture
    def file_client(self):
        """Create a FileAPIClient for testing."""
        from code_indexer.api_clients.file_client import FileAPIClient

        credentials = {"username": "testuser", "password": "testpass"}
        return FileAPIClient(
            server_url="https://test-server.com",
            credentials=credentials,
        )

    @pytest.mark.asyncio
    async def test_delete_file_method_exists(self, file_client):
        """Test that delete_file method exists."""
        assert hasattr(file_client, "delete_file")
        assert callable(file_client.delete_file)

    @pytest.mark.asyncio
    async def test_delete_file_calls_correct_endpoint(self, file_client):
        """Test delete_file calls the correct REST endpoint."""
        with patch.object(
            file_client, "_authenticated_request", new_callable=AsyncMock
        ) as mock_request:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "success": True,
                "file_path": "src/obsolete.py",
                "deleted_at": "2025-01-19T12:00:00Z",
            }
            mock_request.return_value = mock_response

            await file_client.delete_file("test-repo", "src/obsolete.py")

            mock_request.assert_called_once()
            call_args = mock_request.call_args
            assert call_args[0][0] == "DELETE"
            assert "/api/v1/repos/test-repo/files/" in call_args[0][1]

    @pytest.mark.asyncio
    async def test_delete_file_with_content_hash(self, file_client):
        """Test delete_file with content_hash for optimistic locking."""
        with patch.object(
            file_client, "_authenticated_request", new_callable=AsyncMock
        ) as mock_request:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "success": True,
                "file_path": "src/obsolete.py",
                "deleted_at": "2025-01-19T12:00:00Z",
            }
            mock_request.return_value = mock_response

            await file_client.delete_file(
                "test-repo", "src/obsolete.py", content_hash="hash123"
            )

            mock_request.assert_called_once()
            call_args = mock_request.call_args
            # Content hash should be passed as query parameter
            assert call_args[1].get("params", {}).get("content_hash") == "hash123"

    @pytest.mark.asyncio
    async def test_delete_file_without_content_hash(self, file_client):
        """Test delete_file without content_hash."""
        with patch.object(
            file_client, "_authenticated_request", new_callable=AsyncMock
        ) as mock_request:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "success": True,
                "file_path": "src/obsolete.py",
                "deleted_at": "2025-01-19T12:00:00Z",
            }
            mock_request.return_value = mock_response

            await file_client.delete_file("test-repo", "src/obsolete.py")

            mock_request.assert_called_once()
            call_args = mock_request.call_args
            # No params or params without content_hash
            params = call_args[1].get("params")
            assert params is None or not params


class TestFileAPIClientErrorHandling:
    """Tests for error handling in FileAPIClient."""

    @pytest.fixture
    def file_client(self):
        """Create a FileAPIClient for testing."""
        from code_indexer.api_clients.file_client import FileAPIClient

        credentials = {"username": "testuser", "password": "testpass"}
        return FileAPIClient(
            server_url="https://test-server.com",
            credentials=credentials,
        )

    @pytest.mark.asyncio
    async def test_create_file_handles_404(self, file_client):
        """Test create_file handles repository not found error."""
        from code_indexer.api_clients.base_client import APIClientError

        with patch.object(
            file_client, "_authenticated_request", new_callable=AsyncMock
        ) as mock_request:
            mock_response = MagicMock()
            mock_response.status_code = 404
            mock_response.json.return_value = {"detail": "Repository not found"}
            mock_request.return_value = mock_response

            with pytest.raises(APIClientError) as exc_info:
                await file_client.create_file("nonexistent", "file.py", "content")

            assert "not found" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_create_file_handles_409_conflict(self, file_client):
        """Test create_file handles file already exists error."""
        from code_indexer.api_clients.base_client import APIClientError

        with patch.object(
            file_client, "_authenticated_request", new_callable=AsyncMock
        ) as mock_request:
            mock_response = MagicMock()
            mock_response.status_code = 409
            mock_response.json.return_value = {"detail": "File already exists"}
            mock_request.return_value = mock_response

            with pytest.raises(APIClientError) as exc_info:
                await file_client.create_file("repo", "existing.py", "content")

            assert exc_info.value.status_code == 409

    @pytest.mark.asyncio
    async def test_edit_file_handles_409_hash_mismatch(self, file_client):
        """Test edit_file handles hash mismatch error."""
        from code_indexer.api_clients.base_client import APIClientError

        with patch.object(
            file_client, "_authenticated_request", new_callable=AsyncMock
        ) as mock_request:
            mock_response = MagicMock()
            mock_response.status_code = 409
            mock_response.json.return_value = {
                "detail": "Hash mismatch - file was modified"
            }
            mock_request.return_value = mock_response

            with pytest.raises(APIClientError) as exc_info:
                await file_client.edit_file(
                    "repo", "file.py", "old", "new", content_hash="stale_hash"
                )

            assert exc_info.value.status_code == 409
