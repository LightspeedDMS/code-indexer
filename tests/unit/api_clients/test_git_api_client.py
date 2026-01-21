"""
Unit tests for GitAPIClient - Git Workflow Operations API Client.

Story #737: CLI remote mode git workflow operations.
Following TDD methodology - tests written before implementation.
"""

import pytest
from unittest.mock import AsyncMock, MagicMock, patch


class TestGitAPIClientImport:
    """Test that GitAPIClient can be imported."""

    def test_git_api_client_can_be_imported(self):
        """Test GitAPIClient class is importable."""
        from code_indexer.api_clients.git_client import GitAPIClient

        assert GitAPIClient is not None

    def test_git_api_client_inherits_from_base(self):
        """Test GitAPIClient inherits from CIDXRemoteAPIClient."""
        from code_indexer.api_clients.git_client import GitAPIClient
        from code_indexer.api_clients.base_client import CIDXRemoteAPIClient

        assert issubclass(GitAPIClient, CIDXRemoteAPIClient)

    def test_confirmation_required_error_can_be_imported(self):
        """Test ConfirmationRequiredError is importable."""
        from code_indexer.api_clients.git_client import ConfirmationRequiredError

        assert ConfirmationRequiredError is not None


class TestGitAPIClientInitialization:
    """Test GitAPIClient initialization."""

    def test_git_api_client_initialization(self):
        """Test GitAPIClient can be initialized with server_url and credentials."""
        from code_indexer.api_clients.git_client import GitAPIClient

        credentials = {"username": "testuser", "password": "testpass"}
        client = GitAPIClient(
            server_url="https://test-server.com",
            credentials=credentials,
        )

        assert client.server_url == "https://test-server.com"
        assert client.credentials == credentials

    def test_git_api_client_initialization_with_project_root(self, tmp_path):
        """Test GitAPIClient initialization with project_root."""
        from code_indexer.api_clients.git_client import GitAPIClient

        credentials = {"username": "testuser", "password": "testpass"}
        client = GitAPIClient(
            server_url="https://test-server.com",
            credentials=credentials,
            project_root=tmp_path,
        )

        assert client.project_root == tmp_path


class TestGitAPIClientStatusMethods:
    """Tests for git status/inspection methods."""

    @pytest.fixture
    def git_client(self):
        """Create a GitAPIClient for testing."""
        from code_indexer.api_clients.git_client import GitAPIClient

        credentials = {"username": "testuser", "password": "testpass"}
        return GitAPIClient(
            server_url="https://test-server.com",
            credentials=credentials,
        )

    @pytest.mark.asyncio
    async def test_status_method_exists(self, git_client):
        """Test that status method exists."""
        assert hasattr(git_client, "status")
        assert callable(git_client.status)

    @pytest.mark.asyncio
    async def test_status_calls_correct_endpoint(self, git_client):
        """Test status calls the correct REST endpoint."""
        with patch.object(
            git_client, "_authenticated_request", new_callable=AsyncMock
        ) as mock_request:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "success": True,
                "staged": [],
                "unstaged": [],
                "untracked": [],
            }
            mock_request.return_value = mock_response

            _ = await git_client.status("test-repo")

            mock_request.assert_called_once()
            call_args = mock_request.call_args
            assert call_args[0][0] == "GET"
            assert "/api/v1/repos/test-repo/git/status" in call_args[0][1]

    @pytest.mark.asyncio
    async def test_diff_method_exists(self, git_client):
        """Test that diff method exists."""
        assert hasattr(git_client, "diff")
        assert callable(git_client.diff)

    @pytest.mark.asyncio
    async def test_diff_calls_correct_endpoint(self, git_client):
        """Test diff calls the correct REST endpoint."""
        with patch.object(
            git_client, "_authenticated_request", new_callable=AsyncMock
        ) as mock_request:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "success": True,
                "diff_text": "",
                "files_changed": 0,
            }
            mock_request.return_value = mock_response

            _ = await git_client.diff("test-repo")

            mock_request.assert_called_once()
            call_args = mock_request.call_args
            assert call_args[0][0] == "GET"
            assert "/api/v1/repos/test-repo/git/diff" in call_args[0][1]

    @pytest.mark.asyncio
    async def test_log_method_exists(self, git_client):
        """Test that log method exists."""
        assert hasattr(git_client, "log")
        assert callable(git_client.log)

    @pytest.mark.asyncio
    async def test_log_calls_correct_endpoint(self, git_client):
        """Test log calls the correct REST endpoint."""
        with patch.object(
            git_client, "_authenticated_request", new_callable=AsyncMock
        ) as mock_request:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {"success": True, "commits": []}
            mock_request.return_value = mock_response

            await git_client.log("test-repo")

            mock_request.assert_called_once()
            call_args = mock_request.call_args
            assert call_args[0][0] == "GET"
            assert "/api/v1/repos/test-repo/git/log" in call_args[0][1]

    @pytest.mark.asyncio
    async def test_show_commit_method_exists(self, git_client):
        """Test that show_commit method exists."""
        assert hasattr(git_client, "show_commit")
        assert callable(git_client.show_commit)


class TestGitAPIClientStagingMethods:
    """Tests for git staging/commit methods."""

    @pytest.fixture
    def git_client(self):
        """Create a GitAPIClient for testing."""
        from code_indexer.api_clients.git_client import GitAPIClient

        credentials = {"username": "testuser", "password": "testpass"}
        return GitAPIClient(
            server_url="https://test-server.com",
            credentials=credentials,
        )

    @pytest.mark.asyncio
    async def test_stage_method_exists(self, git_client):
        """Test that stage method exists."""
        assert hasattr(git_client, "stage")
        assert callable(git_client.stage)

    @pytest.mark.asyncio
    async def test_stage_calls_correct_endpoint(self, git_client):
        """Test stage calls the correct REST endpoint."""
        with patch.object(
            git_client, "_authenticated_request", new_callable=AsyncMock
        ) as mock_request:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "success": True,
                "staged_files": ["file1.py"],
            }
            mock_request.return_value = mock_response

            await git_client.stage("test-repo", files=["file1.py"])

            mock_request.assert_called_once()
            call_args = mock_request.call_args
            assert call_args[0][0] == "POST"
            assert "/api/v1/repos/test-repo/git/stage" in call_args[0][1]
            assert call_args[1]["json"]["file_paths"] == ["file1.py"]

    @pytest.mark.asyncio
    async def test_unstage_method_exists(self, git_client):
        """Test that unstage method exists."""
        assert hasattr(git_client, "unstage")
        assert callable(git_client.unstage)

    @pytest.mark.asyncio
    async def test_unstage_calls_correct_endpoint(self, git_client):
        """Test unstage calls the correct REST endpoint."""
        with patch.object(
            git_client, "_authenticated_request", new_callable=AsyncMock
        ) as mock_request:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "success": True,
                "unstaged_files": ["file1.py"],
            }
            mock_request.return_value = mock_response

            await git_client.unstage("test-repo", files=["file1.py"])

            mock_request.assert_called_once()
            call_args = mock_request.call_args
            assert call_args[0][0] == "POST"
            assert "/api/v1/repos/test-repo/git/unstage" in call_args[0][1]
            assert call_args[1]["json"]["file_paths"] == ["file1.py"]

    @pytest.mark.asyncio
    async def test_commit_method_exists(self, git_client):
        """Test that commit method exists."""
        assert hasattr(git_client, "commit")
        assert callable(git_client.commit)

    @pytest.mark.asyncio
    async def test_commit_calls_correct_endpoint(self, git_client):
        """Test commit calls the correct REST endpoint."""
        with patch.object(
            git_client, "_authenticated_request", new_callable=AsyncMock
        ) as mock_request:
            mock_response = MagicMock()
            mock_response.status_code = 201
            mock_response.json.return_value = {
                "success": True,
                "commit_hash": "abc123def456",
                "short_hash": "abc123d",
                "message": "Test commit",
                "author": "test@example.com",
                "files_committed": 1,
            }
            mock_request.return_value = mock_response

            await git_client.commit("test-repo", message="Test commit")

            mock_request.assert_called_once()
            call_args = mock_request.call_args
            assert call_args[0][0] == "POST"
            assert "/api/v1/repos/test-repo/git/commit" in call_args[0][1]
            assert call_args[1]["json"]["message"] == "Test commit"


class TestGitAPIClientRemoteMethods:
    """Tests for git remote operations methods."""

    @pytest.fixture
    def git_client(self):
        """Create a GitAPIClient for testing."""
        from code_indexer.api_clients.git_client import GitAPIClient

        credentials = {"username": "testuser", "password": "testpass"}
        return GitAPIClient(
            server_url="https://test-server.com",
            credentials=credentials,
        )

    @pytest.mark.asyncio
    async def test_push_method_exists(self, git_client):
        """Test that push method exists."""
        assert hasattr(git_client, "push")
        assert callable(git_client.push)

    @pytest.mark.asyncio
    async def test_push_calls_correct_endpoint(self, git_client):
        """Test push calls the correct REST endpoint."""
        with patch.object(
            git_client, "_authenticated_request", new_callable=AsyncMock
        ) as mock_request:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "success": True,
                "branch": "main",
                "remote": "origin",
                "commits_pushed": 1,
            }
            mock_request.return_value = mock_response

            await git_client.push("test-repo")

            mock_request.assert_called_once()
            call_args = mock_request.call_args
            assert call_args[0][0] == "POST"
            assert "/api/v1/repos/test-repo/git/push" in call_args[0][1]

    @pytest.mark.asyncio
    async def test_pull_method_exists(self, git_client):
        """Test that pull method exists."""
        assert hasattr(git_client, "pull")
        assert callable(git_client.pull)

    @pytest.mark.asyncio
    async def test_pull_calls_correct_endpoint(self, git_client):
        """Test pull calls the correct REST endpoint."""
        with patch.object(
            git_client, "_authenticated_request", new_callable=AsyncMock
        ) as mock_request:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "success": True,
                "updated_files": 0,
                "conflicts": [],
            }
            mock_request.return_value = mock_response

            await git_client.pull("test-repo")

            mock_request.assert_called_once()
            call_args = mock_request.call_args
            assert call_args[0][0] == "POST"
            assert "/api/v1/repos/test-repo/git/pull" in call_args[0][1]

    @pytest.mark.asyncio
    async def test_fetch_method_exists(self, git_client):
        """Test that fetch method exists."""
        assert hasattr(git_client, "fetch")
        assert callable(git_client.fetch)

    @pytest.mark.asyncio
    async def test_fetch_calls_correct_endpoint(self, git_client):
        """Test fetch calls the correct REST endpoint."""
        with patch.object(
            git_client, "_authenticated_request", new_callable=AsyncMock
        ) as mock_request:
            mock_response = MagicMock()
            mock_response.status_code = 200
            mock_response.json.return_value = {
                "success": True,
                "fetched_refs": [],
            }
            mock_request.return_value = mock_response

            await git_client.fetch("test-repo")

            mock_request.assert_called_once()
            call_args = mock_request.call_args
            assert call_args[0][0] == "POST"
            assert "/api/v1/repos/test-repo/git/fetch" in call_args[0][1]
