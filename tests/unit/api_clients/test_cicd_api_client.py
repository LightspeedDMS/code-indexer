"""
Unit tests for CICDAPIClient - CI/CD Monitoring API Client.

Story #746: CLI remote mode CI/CD monitoring.
Following TDD methodology - tests written before implementation.
"""

import pytest
from unittest.mock import MagicMock, patch


class TestCICDAPIClientImport:
    """Test that CICDAPIClient can be imported."""

    def test_cicd_api_client_can_be_imported(self):
        """Test CICDAPIClient class is importable."""
        from code_indexer.api_clients.cicd_client import CICDAPIClient

        assert CICDAPIClient is not None

    def test_cicd_api_client_inherits_from_base(self):
        """Test CICDAPIClient inherits from CIDXRemoteAPIClient."""
        from code_indexer.api_clients.cicd_client import CICDAPIClient
        from code_indexer.api_clients.base_client import CIDXRemoteAPIClient

        assert issubclass(CICDAPIClient, CIDXRemoteAPIClient)


class TestCICDAPIClientInitialization:
    """Test CICDAPIClient initialization."""

    def test_cicd_api_client_initialization(self):
        """Test CICDAPIClient can be initialized with server_url and credentials."""
        from code_indexer.api_clients.cicd_client import CICDAPIClient

        credentials = {"username": "testuser", "password": "testpass"}
        client = CICDAPIClient(
            server_url="https://test-server.com",
            credentials=credentials,
        )

        assert client.server_url == "https://test-server.com"
        assert client.credentials == credentials

    def test_cicd_api_client_initialization_with_project_root(self, tmp_path):
        """Test CICDAPIClient initialization with project_root."""
        from code_indexer.api_clients.cicd_client import CICDAPIClient

        credentials = {"username": "testuser", "password": "testpass"}
        client = CICDAPIClient(
            server_url="https://test-server.com",
            credentials=credentials,
            project_root=tmp_path,
        )

        assert client.project_root == tmp_path


class TestCICDAPIClientGitHubMethods:
    """Tests for GitHub Actions CI/CD methods."""

    @pytest.fixture
    def cicd_client(self):
        """Create a CICDAPIClient for testing."""
        from code_indexer.api_clients.cicd_client import CICDAPIClient

        credentials = {"username": "testuser", "password": "testpass"}
        return CICDAPIClient(
            server_url="https://test-server.com",
            credentials=credentials,
        )

    # GitHub list runs tests
    @pytest.mark.asyncio
    async def test_github_list_runs_method_exists(self, cicd_client):
        """Test that github_list_runs method exists."""
        assert hasattr(cicd_client, "github_list_runs")
        assert callable(cicd_client.github_list_runs)

    @pytest.mark.asyncio
    async def test_github_list_runs_calls_correct_endpoint(self, cicd_client):
        """Test github_list_runs calls the correct REST endpoint."""
        with patch.object(
            cicd_client, "_authenticated_request", new_callable=MagicMock
        ) as mock_request:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "runs": [],
                "total_count": 0,
            }
            mock_request.return_value = mock_response

            cicd_client.github_list_runs("owner", "repo")

            mock_request.assert_called_once()
            call_args = mock_request.call_args
            assert call_args[0][0] == "GET"
            assert "/api/cicd/github/owner/repo/runs" in call_args[0][1]

    @pytest.mark.asyncio
    async def test_github_list_runs_with_filters(self, cicd_client):
        """Test github_list_runs passes filter parameters."""
        with patch.object(
            cicd_client, "_authenticated_request", new_callable=MagicMock
        ) as mock_request:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"runs": [], "total_count": 0}
            mock_request.return_value = mock_response

            cicd_client.github_list_runs(
                "owner", "repo", status="failure", branch="main", limit=5
            )

            mock_request.assert_called_once()
            call_args = mock_request.call_args
            params = call_args[1].get("params", {})
            assert params.get("status") == "failure"
            assert params.get("branch") == "main"
            assert params.get("limit") == 5

    # GitHub get run tests
    @pytest.mark.asyncio
    async def test_github_get_run_method_exists(self, cicd_client):
        """Test that github_get_run method exists."""
        assert hasattr(cicd_client, "github_get_run")
        assert callable(cicd_client.github_get_run)

    @pytest.mark.asyncio
    async def test_github_get_run_calls_correct_endpoint(self, cicd_client):
        """Test github_get_run calls the correct REST endpoint."""
        with patch.object(
            cicd_client, "_authenticated_request", new_callable=MagicMock
        ) as mock_request:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "id": 12345,
                "status": "completed",
                "conclusion": "success",
            }
            mock_request.return_value = mock_response

            cicd_client.github_get_run("owner", "repo", 12345)

            mock_request.assert_called_once()
            call_args = mock_request.call_args
            assert call_args[0][0] == "GET"
            assert "/api/cicd/github/owner/repo/runs/12345" in call_args[0][1]

    # GitHub search logs tests
    @pytest.mark.asyncio
    async def test_github_search_logs_method_exists(self, cicd_client):
        """Test that github_search_logs method exists."""
        assert hasattr(cicd_client, "github_search_logs")
        assert callable(cicd_client.github_search_logs)

    @pytest.mark.asyncio
    async def test_github_search_logs_calls_correct_endpoint(self, cicd_client):
        """Test github_search_logs calls the correct REST endpoint."""
        with patch.object(
            cicd_client, "_authenticated_request", new_callable=MagicMock
        ) as mock_request:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"matches": [], "total_matches": 0}
            mock_request.return_value = mock_response

            cicd_client.github_search_logs("owner", "repo", 12345)

            mock_request.assert_called_once()
            call_args = mock_request.call_args
            assert call_args[0][0] == "GET"
            assert "/api/cicd/github/owner/repo/runs/12345/logs" in call_args[0][1]

    @pytest.mark.asyncio
    async def test_github_search_logs_with_query(self, cicd_client):
        """Test github_search_logs passes query parameter."""
        with patch.object(
            cicd_client, "_authenticated_request", new_callable=MagicMock
        ) as mock_request:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"matches": [], "total_matches": 0}
            mock_request.return_value = mock_response

            cicd_client.github_search_logs("owner", "repo", 12345, query="error")

            mock_request.assert_called_once()
            call_args = mock_request.call_args
            params = call_args[1].get("params", {})
            assert params.get("query") == "error"

    # GitHub get job logs tests
    @pytest.mark.asyncio
    async def test_github_get_job_logs_method_exists(self, cicd_client):
        """Test that github_get_job_logs method exists."""
        assert hasattr(cicd_client, "github_get_job_logs")
        assert callable(cicd_client.github_get_job_logs)

    @pytest.mark.asyncio
    async def test_github_get_job_logs_calls_correct_endpoint(self, cicd_client):
        """Test github_get_job_logs calls the correct REST endpoint."""
        with patch.object(
            cicd_client, "_authenticated_request", new_callable=MagicMock
        ) as mock_request:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"logs": "log content here"}
            mock_request.return_value = mock_response

            cicd_client.github_get_job_logs("owner", "repo", 67890)

            mock_request.assert_called_once()
            call_args = mock_request.call_args
            assert call_args[0][0] == "GET"
            assert "/api/cicd/github/owner/repo/jobs/67890/logs" in call_args[0][1]

    # GitHub retry run tests
    @pytest.mark.asyncio
    async def test_github_retry_run_method_exists(self, cicd_client):
        """Test that github_retry_run method exists."""
        assert hasattr(cicd_client, "github_retry_run")
        assert callable(cicd_client.github_retry_run)

    @pytest.mark.asyncio
    async def test_github_retry_run_calls_correct_endpoint(self, cicd_client):
        """Test github_retry_run calls the correct REST endpoint."""
        with patch.object(
            cicd_client, "_authenticated_request", new_callable=MagicMock
        ) as mock_request:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "success": True,
                "message": "Run restarted",
            }
            mock_request.return_value = mock_response

            cicd_client.github_retry_run("owner", "repo", 12345)

            mock_request.assert_called_once()
            call_args = mock_request.call_args
            assert call_args[0][0] == "POST"
            assert "/api/cicd/github/owner/repo/runs/12345/retry" in call_args[0][1]

    # GitHub cancel run tests
    @pytest.mark.asyncio
    async def test_github_cancel_run_method_exists(self, cicd_client):
        """Test that github_cancel_run method exists."""
        assert hasattr(cicd_client, "github_cancel_run")
        assert callable(cicd_client.github_cancel_run)

    @pytest.mark.asyncio
    async def test_github_cancel_run_calls_correct_endpoint(self, cicd_client):
        """Test github_cancel_run calls the correct REST endpoint."""
        with patch.object(
            cicd_client, "_authenticated_request", new_callable=MagicMock
        ) as mock_request:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "success": True,
                "message": "Run cancelled",
            }
            mock_request.return_value = mock_response

            cicd_client.github_cancel_run("owner", "repo", 12345)

            mock_request.assert_called_once()
            call_args = mock_request.call_args
            assert call_args[0][0] == "POST"
            assert "/api/cicd/github/owner/repo/runs/12345/cancel" in call_args[0][1]


class TestCICDAPIClientGitLabMethods:
    """Tests for GitLab CI methods."""

    @pytest.fixture
    def cicd_client(self):
        """Create a CICDAPIClient for testing."""
        from code_indexer.api_clients.cicd_client import CICDAPIClient

        credentials = {"username": "testuser", "password": "testpass"}
        return CICDAPIClient(
            server_url="https://test-server.com",
            credentials=credentials,
        )

    # GitLab list pipelines tests
    @pytest.mark.asyncio
    async def test_gitlab_list_pipelines_method_exists(self, cicd_client):
        """Test that gitlab_list_pipelines method exists."""
        assert hasattr(cicd_client, "gitlab_list_pipelines")
        assert callable(cicd_client.gitlab_list_pipelines)

    @pytest.mark.asyncio
    async def test_gitlab_list_pipelines_calls_correct_endpoint(self, cicd_client):
        """Test gitlab_list_pipelines calls the correct REST endpoint."""
        with patch.object(
            cicd_client, "_authenticated_request", new_callable=MagicMock
        ) as mock_request:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"pipelines": [], "total_count": 0}
            mock_request.return_value = mock_response

            cicd_client.gitlab_list_pipelines("my-project")

            mock_request.assert_called_once()
            call_args = mock_request.call_args
            assert call_args[0][0] == "GET"
            assert "/api/cicd/gitlab/my-project/pipelines" in call_args[0][1]

    @pytest.mark.asyncio
    async def test_gitlab_list_pipelines_with_filters(self, cicd_client):
        """Test gitlab_list_pipelines passes filter parameters."""
        with patch.object(
            cicd_client, "_authenticated_request", new_callable=MagicMock
        ) as mock_request:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"pipelines": [], "total_count": 0}
            mock_request.return_value = mock_response

            cicd_client.gitlab_list_pipelines(
                "my-project", status="failed", ref="main", limit=5
            )

            mock_request.assert_called_once()
            call_args = mock_request.call_args
            params = call_args[1].get("params", {})
            assert params.get("status") == "failed"
            assert params.get("ref") == "main"
            assert params.get("limit") == 5

    # GitLab get pipeline tests
    @pytest.mark.asyncio
    async def test_gitlab_get_pipeline_method_exists(self, cicd_client):
        """Test that gitlab_get_pipeline method exists."""
        assert hasattr(cicd_client, "gitlab_get_pipeline")
        assert callable(cicd_client.gitlab_get_pipeline)

    @pytest.mark.asyncio
    async def test_gitlab_get_pipeline_calls_correct_endpoint(self, cicd_client):
        """Test gitlab_get_pipeline calls the correct REST endpoint."""
        with patch.object(
            cicd_client, "_authenticated_request", new_callable=MagicMock
        ) as mock_request:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "id": 12345,
                "status": "success",
                "ref": "main",
            }
            mock_request.return_value = mock_response

            cicd_client.gitlab_get_pipeline("my-project", 12345)

            mock_request.assert_called_once()
            call_args = mock_request.call_args
            assert call_args[0][0] == "GET"
            assert "/api/cicd/gitlab/my-project/pipelines/12345" in call_args[0][1]

    # GitLab search logs tests
    @pytest.mark.asyncio
    async def test_gitlab_search_logs_method_exists(self, cicd_client):
        """Test that gitlab_search_logs method exists."""
        assert hasattr(cicd_client, "gitlab_search_logs")
        assert callable(cicd_client.gitlab_search_logs)

    @pytest.mark.asyncio
    async def test_gitlab_search_logs_calls_correct_endpoint(self, cicd_client):
        """Test gitlab_search_logs calls the correct REST endpoint."""
        with patch.object(
            cicd_client, "_authenticated_request", new_callable=MagicMock
        ) as mock_request:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"matches": [], "total_matches": 0}
            mock_request.return_value = mock_response

            cicd_client.gitlab_search_logs("my-project", 12345)

            mock_request.assert_called_once()
            call_args = mock_request.call_args
            assert call_args[0][0] == "GET"
            assert "/api/cicd/gitlab/my-project/pipelines/12345/logs" in call_args[0][1]

    @pytest.mark.asyncio
    async def test_gitlab_search_logs_with_query(self, cicd_client):
        """Test gitlab_search_logs passes query parameter."""
        with patch.object(
            cicd_client, "_authenticated_request", new_callable=MagicMock
        ) as mock_request:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"matches": [], "total_matches": 0}
            mock_request.return_value = mock_response

            cicd_client.gitlab_search_logs("my-project", 12345, query="error")

            mock_request.assert_called_once()
            call_args = mock_request.call_args
            params = call_args[1].get("params", {})
            assert params.get("query") == "error"

    # GitLab get job logs tests
    @pytest.mark.asyncio
    async def test_gitlab_get_job_logs_method_exists(self, cicd_client):
        """Test that gitlab_get_job_logs method exists."""
        assert hasattr(cicd_client, "gitlab_get_job_logs")
        assert callable(cicd_client.gitlab_get_job_logs)

    @pytest.mark.asyncio
    async def test_gitlab_get_job_logs_calls_correct_endpoint(self, cicd_client):
        """Test gitlab_get_job_logs calls the correct REST endpoint."""
        with patch.object(
            cicd_client, "_authenticated_request", new_callable=MagicMock
        ) as mock_request:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"logs": "log content here"}
            mock_request.return_value = mock_response

            cicd_client.gitlab_get_job_logs("my-project", 67890)

            mock_request.assert_called_once()
            call_args = mock_request.call_args
            assert call_args[0][0] == "GET"
            assert "/api/cicd/gitlab/my-project/jobs/67890/logs" in call_args[0][1]

    # GitLab retry pipeline tests
    @pytest.mark.asyncio
    async def test_gitlab_retry_pipeline_method_exists(self, cicd_client):
        """Test that gitlab_retry_pipeline method exists."""
        assert hasattr(cicd_client, "gitlab_retry_pipeline")
        assert callable(cicd_client.gitlab_retry_pipeline)

    @pytest.mark.asyncio
    async def test_gitlab_retry_pipeline_calls_correct_endpoint(self, cicd_client):
        """Test gitlab_retry_pipeline calls the correct REST endpoint."""
        with patch.object(
            cicd_client, "_authenticated_request", new_callable=MagicMock
        ) as mock_request:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "success": True,
                "message": "Pipeline retried",
            }
            mock_request.return_value = mock_response

            cicd_client.gitlab_retry_pipeline("my-project", 12345)

            mock_request.assert_called_once()
            call_args = mock_request.call_args
            assert call_args[0][0] == "POST"
            assert (
                "/api/cicd/gitlab/my-project/pipelines/12345/retry" in call_args[0][1]
            )

    # GitLab cancel pipeline tests
    @pytest.mark.asyncio
    async def test_gitlab_cancel_pipeline_method_exists(self, cicd_client):
        """Test that gitlab_cancel_pipeline method exists."""
        assert hasattr(cicd_client, "gitlab_cancel_pipeline")
        assert callable(cicd_client.gitlab_cancel_pipeline)

    @pytest.mark.asyncio
    async def test_gitlab_cancel_pipeline_calls_correct_endpoint(self, cicd_client):
        """Test gitlab_cancel_pipeline calls the correct REST endpoint."""
        with patch.object(
            cicd_client, "_authenticated_request", new_callable=MagicMock
        ) as mock_request:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "success": True,
                "message": "Pipeline cancelled",
            }
            mock_request.return_value = mock_response

            cicd_client.gitlab_cancel_pipeline("my-project", 12345)

            mock_request.assert_called_once()
            call_args = mock_request.call_args
            assert call_args[0][0] == "POST"
            assert (
                "/api/cicd/gitlab/my-project/pipelines/12345/cancel" in call_args[0][1]
            )


class TestCICDAPIClientErrorHandling:
    """Tests for error handling in CICDAPIClient."""

    @pytest.fixture
    def cicd_client(self):
        """Create a CICDAPIClient for testing."""
        from code_indexer.api_clients.cicd_client import CICDAPIClient

        credentials = {"username": "testuser", "password": "testpass"}
        return CICDAPIClient(
            server_url="https://test-server.com",
            credentials=credentials,
        )

    @pytest.mark.asyncio
    async def test_github_list_runs_404_error(self, cicd_client):
        """Test github_list_runs handles 404 errors."""
        from code_indexer.api_clients.base_client import APIClientError

        with patch.object(
            cicd_client, "_authenticated_request", new_callable=MagicMock
        ) as mock_request:
            mock_response = MagicMock()
            mock_response.status_code = 404
            mock_response.json.return_value = {"detail": "Repository not found"}
            mock_request.return_value = mock_response

            with pytest.raises(APIClientError) as exc_info:
                cicd_client.github_list_runs("owner", "repo")

            assert "not found" in str(exc_info.value).lower()

    @pytest.mark.asyncio
    async def test_gitlab_list_pipelines_404_error(self, cicd_client):
        """Test gitlab_list_pipelines handles 404 errors."""
        from code_indexer.api_clients.base_client import APIClientError

        with patch.object(
            cicd_client, "_authenticated_request", new_callable=MagicMock
        ) as mock_request:
            mock_response = MagicMock()
            mock_response.status_code = 404
            mock_response.json.return_value = {"detail": "Project not found"}
            mock_request.return_value = mock_response

            with pytest.raises(APIClientError) as exc_info:
                cicd_client.gitlab_list_pipelines("my-project")

            assert "not found" in str(exc_info.value).lower()
