"""
Unit tests for IndexAPIClient - Index Management API Client.

Story #656: Advanced Operations Parity - Indexing Commands.
Following TDD methodology - tests written before implementation.
"""

import pytest
from unittest.mock import MagicMock, patch


class TestIndexAPIClientImport:
    """Test that IndexAPIClient can be imported."""

    def test_index_api_client_can_be_imported(self):
        """Test IndexAPIClient class is importable."""
        from code_indexer.api_clients.index_client import IndexAPIClient

        assert IndexAPIClient is not None

    def test_index_api_client_inherits_from_base(self):
        """Test IndexAPIClient inherits from CIDXRemoteAPIClient."""
        from code_indexer.api_clients.index_client import IndexAPIClient
        from code_indexer.api_clients.base_client import CIDXRemoteAPIClient

        assert issubclass(IndexAPIClient, CIDXRemoteAPIClient)


class TestIndexAPIClientInitialization:
    """Test IndexAPIClient initialization."""

    def test_index_api_client_initialization(self):
        """Test IndexAPIClient can be initialized with server_url and credentials."""
        from code_indexer.api_clients.index_client import IndexAPIClient

        credentials = {"username": "testuser", "password": "testpass"}
        client = IndexAPIClient(
            server_url="https://test-server.com",
            credentials=credentials,
        )

        assert client.server_url == "https://test-server.com"
        assert client.credentials == credentials

    def test_index_api_client_initialization_with_project_root(self, tmp_path):
        """Test IndexAPIClient initialization with project_root."""
        from code_indexer.api_clients.index_client import IndexAPIClient

        credentials = {"username": "testuser", "password": "testpass"}
        client = IndexAPIClient(
            server_url="https://test-server.com",
            credentials=credentials,
            project_root=tmp_path,
        )

        assert client.project_root == tmp_path


class TestIndexAPIClientTriggerMethod:
    """Tests for index trigger method."""

    @pytest.fixture
    def index_client(self):
        """Create an IndexAPIClient for testing."""
        from code_indexer.api_clients.index_client import IndexAPIClient

        credentials = {"username": "testuser", "password": "testpass"}
        return IndexAPIClient(
            server_url="https://test-server.com",
            credentials=credentials,
        )

    @pytest.mark.asyncio
    async def test_trigger_method_exists(self, index_client):
        """Test that trigger method exists."""
        assert hasattr(index_client, "trigger")
        assert callable(index_client.trigger)

    @pytest.mark.asyncio
    async def test_trigger_calls_correct_endpoint(self, index_client):
        """Test trigger calls the correct REST endpoint."""
        with patch.object(
            index_client, "_authenticated_request", new_callable=MagicMock
        ) as mock_request:
            mock_response = MagicMock()
            mock_response.status_code = 202
            mock_response.json.return_value = {
                "job_id": "job-123",
                "repository": "my-repo",
                "status": "queued",
                "index_types": ["semantic", "fts"],
            }
            mock_request.return_value = mock_response

            _ = index_client.trigger("my-repo")

            mock_request.assert_called_once()
            call_args = mock_request.call_args
            assert call_args[0][0] == "POST"
            assert "/api/v1/index/my-repo/trigger" in call_args[0][1]

    @pytest.mark.asyncio
    async def test_trigger_with_clear_option(self, index_client):
        """Test trigger with clear option."""
        with patch.object(
            index_client, "_authenticated_request", new_callable=MagicMock
        ) as mock_request:
            mock_response = MagicMock()
            mock_response.status_code = 202
            mock_response.json.return_value = {
                "job_id": "job-123",
                "repository": "my-repo",
                "status": "queued",
                "cleared": True,
            }
            mock_request.return_value = mock_response

            index_client.trigger("my-repo", clear=True)

            mock_request.assert_called_once()
            call_args = mock_request.call_args
            assert call_args[1]["json"]["clear"] is True

    @pytest.mark.asyncio
    async def test_trigger_with_index_types(self, index_client):
        """Test trigger with specific index types."""
        with patch.object(
            index_client, "_authenticated_request", new_callable=MagicMock
        ) as mock_request:
            mock_response = MagicMock()
            mock_response.status_code = 202
            mock_response.json.return_value = {
                "job_id": "job-123",
                "repository": "my-repo",
                "status": "queued",
                "index_types": ["semantic", "scip"],
            }
            mock_request.return_value = mock_response

            index_client.trigger(
                "my-repo",
                index_types=["semantic", "scip"],
            )

            mock_request.assert_called_once()
            call_args = mock_request.call_args
            assert call_args[1]["json"]["types"] == ["semantic", "scip"]

    @pytest.mark.asyncio
    async def test_trigger_returns_job_info(self, index_client):
        """Test trigger returns job information."""
        with patch.object(
            index_client, "_authenticated_request", new_callable=MagicMock
        ) as mock_request:
            mock_response = MagicMock()
            mock_response.status_code = 202
            mock_response.json.return_value = {
                "job_id": "job-456",
                "repository": "my-repo",
                "status": "queued",
                "index_types": ["semantic", "fts", "temporal"],
            }
            mock_request.return_value = mock_response

            result = index_client.trigger("my-repo")

            assert "job_id" in result
            assert result["job_id"] == "job-456"
            assert result["status"] == "queued"


class TestIndexAPIClientStatusMethod:
    """Tests for index status method."""

    @pytest.fixture
    def index_client(self):
        """Create an IndexAPIClient for testing."""
        from code_indexer.api_clients.index_client import IndexAPIClient

        credentials = {"username": "testuser", "password": "testpass"}
        return IndexAPIClient(
            server_url="https://test-server.com",
            credentials=credentials,
        )

    @pytest.mark.asyncio
    async def test_status_method_exists(self, index_client):
        """Test that status method exists."""
        assert hasattr(index_client, "status")
        assert callable(index_client.status)

    @pytest.mark.asyncio
    async def test_status_calls_correct_endpoint(self, index_client):
        """Test status calls the correct REST endpoint."""
        with patch.object(
            index_client, "_authenticated_request", new_callable=MagicMock
        ) as mock_request:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "repository": "my-repo",
                "indexes": {
                    "semantic": {
                        "status": "complete",
                        "files_indexed": 150,
                        "last_updated": "2025-01-20T00:00:00Z",
                    },
                    "fts": {
                        "status": "complete",
                        "files_indexed": 150,
                        "last_updated": "2025-01-20T00:00:00Z",
                    },
                    "temporal": {
                        "status": "not_configured",
                    },
                    "scip": {
                        "status": "complete",
                        "projects_indexed": 3,
                    },
                },
            }
            mock_request.return_value = mock_response

            _ = index_client.status("my-repo")

            mock_request.assert_called_once()
            call_args = mock_request.call_args
            assert call_args[0][0] == "GET"
            assert "/api/v1/index/my-repo/status" in call_args[0][1]

    @pytest.mark.asyncio
    async def test_status_returns_index_information(self, index_client):
        """Test status returns detailed index information."""
        with patch.object(
            index_client, "_authenticated_request", new_callable=MagicMock
        ) as mock_request:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "repository": "my-repo",
                "indexes": {
                    "semantic": {"status": "complete", "files_indexed": 100},
                    "fts": {"status": "in_progress", "progress": 75},
                },
            }
            mock_request.return_value = mock_response

            result = index_client.status("my-repo")

            assert "repository" in result
            assert "indexes" in result
            assert "semantic" in result["indexes"]
            assert result["indexes"]["semantic"]["status"] == "complete"


class TestIndexAPIClientAddTypeMethod:
    """Tests for index add-type method."""

    @pytest.fixture
    def index_client(self):
        """Create an IndexAPIClient for testing."""
        from code_indexer.api_clients.index_client import IndexAPIClient

        credentials = {"username": "testuser", "password": "testpass"}
        return IndexAPIClient(
            server_url="https://test-server.com",
            credentials=credentials,
        )

    @pytest.mark.asyncio
    async def test_add_type_method_exists(self, index_client):
        """Test that add_type method exists."""
        assert hasattr(index_client, "add_type")
        assert callable(index_client.add_type)

    @pytest.mark.asyncio
    async def test_add_type_calls_correct_endpoint(self, index_client):
        """Test add_type calls the correct REST endpoint."""
        with patch.object(
            index_client, "_authenticated_request", new_callable=MagicMock
        ) as mock_request:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "added": True,
                "repository": "my-repo",
                "type": "temporal",
                "job_id": "job-789",
            }
            mock_request.return_value = mock_response

            _ = index_client.add_type("my-repo", "temporal")

            mock_request.assert_called_once()
            call_args = mock_request.call_args
            assert call_args[0][0] == "POST"
            assert "/api/v1/index/my-repo/add-type" in call_args[0][1]
            assert call_args[1]["json"]["type"] == "temporal"

    @pytest.mark.asyncio
    async def test_add_type_returns_result(self, index_client):
        """Test add_type returns the operation result."""
        with patch.object(
            index_client, "_authenticated_request", new_callable=MagicMock
        ) as mock_request:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "added": True,
                "repository": "my-repo",
                "type": "scip",
                "job_id": "job-abc",
            }
            mock_request.return_value = mock_response

            result = index_client.add_type("my-repo", "scip")

            assert result["added"] is True
            assert result["type"] == "scip"
            assert "job_id" in result


class TestIndexAPIClientErrorHandling:
    """Tests for Index API client error handling."""

    @pytest.fixture
    def index_client(self):
        """Create an IndexAPIClient for testing."""
        from code_indexer.api_clients.index_client import IndexAPIClient

        credentials = {"username": "testuser", "password": "testpass"}
        return IndexAPIClient(
            server_url="https://test-server.com",
            credentials=credentials,
        )

    @pytest.mark.asyncio
    async def test_trigger_handles_not_found_error(self, index_client):
        """Test trigger handles 404 not found."""
        from code_indexer.api_clients.base_client import APIClientError

        with patch.object(
            index_client, "_authenticated_request", new_callable=MagicMock
        ) as mock_request:
            mock_response = MagicMock()
            mock_response.status_code = 404
            mock_response.json.return_value = {
                "detail": "Repository 'nonexistent' not found"
            }
            mock_request.return_value = mock_response

            with pytest.raises(APIClientError) as exc_info:
                index_client.trigger("nonexistent")

            assert "not found" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_status_handles_not_found_error(self, index_client):
        """Test status handles 404 not found."""
        from code_indexer.api_clients.base_client import APIClientError

        with patch.object(
            index_client, "_authenticated_request", new_callable=MagicMock
        ) as mock_request:
            mock_response = MagicMock()
            mock_response.status_code = 404
            mock_response.json.return_value = {
                "detail": "Repository 'nonexistent' not found"
            }
            mock_request.return_value = mock_response

            with pytest.raises(APIClientError) as exc_info:
                index_client.status("nonexistent")

            assert "not found" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_add_type_handles_invalid_type_error(self, index_client):
        """Test add_type handles 400 bad request for invalid type."""
        from code_indexer.api_clients.base_client import APIClientError

        with patch.object(
            index_client, "_authenticated_request", new_callable=MagicMock
        ) as mock_request:
            mock_response = MagicMock()
            mock_response.status_code = 400
            mock_response.json.return_value = {
                "detail": "Invalid index type 'invalid'. Valid types: semantic, fts, temporal, scip"
            }
            mock_request.return_value = mock_response

            with pytest.raises(APIClientError) as exc_info:
                index_client.add_type("my-repo", "invalid")

            assert "invalid" in str(exc_info.value).lower()
