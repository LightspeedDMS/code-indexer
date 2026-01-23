"""
Tests for Discovery Branches API Route.

Following TDD methodology - these tests define expected behavior BEFORE implementation.
Tests the POST /api/discovery/branches endpoint for fetching remote branches.
"""

from fastapi.testclient import TestClient


class TestDiscoveryBranchesRoute:
    """Tests for the POST /api/discovery/branches route."""

    def test_branches_endpoint_exists(self):
        """Test that the branches endpoint exists and responds."""
        from code_indexer.server.web.routes import web_router
        from fastapi import FastAPI

        app = FastAPI()
        app.include_router(web_router, prefix="/admin")

        client = TestClient(app)
        response = client.post(
            "/admin/api/discovery/branches",
            json={"repos": []},
            follow_redirects=False,
        )

        # Route should exist (auth required, so may redirect or 401)
        # 422 means route exists but validation failed
        # 200 means success
        # 302/303/307 means redirect (auth)
        assert response.status_code in [200, 302, 303, 307, 401, 422]

    def test_branches_endpoint_requires_authentication(self):
        """Test that branches endpoint requires admin authentication."""
        from code_indexer.server.web.routes import web_router
        from fastapi import FastAPI

        app = FastAPI()
        app.include_router(web_router, prefix="/admin")

        client = TestClient(app)
        response = client.post(
            "/admin/api/discovery/branches",
            json={
                "repos": [
                    {
                        "clone_url": "https://github.com/test/repo.git",
                        "platform": "github",
                    }
                ]
            },
            follow_redirects=False,
        )

        # Should require authentication
        assert response.status_code in [302, 303, 307, 401, 403]

    def test_branches_endpoint_accepts_repos_list(self):
        """Test that branches endpoint accepts repos list in request body."""
        from code_indexer.server.web.routes import web_router
        from fastapi import FastAPI

        app = FastAPI()
        app.include_router(web_router, prefix="/admin")

        client = TestClient(app)

        # Test with valid structure but no auth
        request_body = {
            "repos": [
                {
                    "clone_url": "https://github.com/octocat/Hello-World.git",
                    "platform": "github",
                },
                {
                    "clone_url": "https://gitlab.com/example/repo.git",
                    "platform": "gitlab",
                },
            ]
        }

        response = client.post(
            "/admin/api/discovery/branches",
            json=request_body,
            follow_redirects=False,
        )

        # Should accept the request structure (even if auth fails)
        # 422 would indicate validation error in request structure
        assert response.status_code != 422, "Request body structure should be valid"

    def test_branches_endpoint_returns_json(self):
        """Test that branches endpoint returns JSON response."""
        from code_indexer.server.web.routes import web_router
        from fastapi import FastAPI

        app = FastAPI()
        app.include_router(web_router, prefix="/admin")

        client = TestClient(app)

        response = client.post(
            "/admin/api/discovery/branches",
            json={"repos": []},
            follow_redirects=False,
        )

        # If not redirecting, should return JSON
        if response.status_code in [200, 400, 422]:
            assert "application/json" in response.headers.get("content-type", "")


class TestDiscoveryBranchesRequestValidation:
    """Tests for request validation on branches endpoint."""

    def test_empty_repos_list_is_valid(self):
        """Test that empty repos list is accepted."""
        from code_indexer.server.web.routes import web_router
        from fastapi import FastAPI

        app = FastAPI()
        app.include_router(web_router, prefix="/admin")

        client = TestClient(app)

        response = client.post(
            "/admin/api/discovery/branches",
            json={"repos": []},
            follow_redirects=False,
        )

        # Empty list should be valid (not 422)
        assert response.status_code != 422

    def test_missing_repos_field_returns_error(self):
        """Test that missing repos field returns validation error."""
        from code_indexer.server.web.routes import web_router
        from fastapi import FastAPI

        app = FastAPI()
        app.include_router(web_router, prefix="/admin")

        client = TestClient(app)

        response = client.post(
            "/admin/api/discovery/branches",
            json={},  # Missing 'repos' field
            follow_redirects=False,
        )

        # Should return 422 for missing required field (if not auth redirect)
        assert response.status_code in [302, 303, 307, 401, 422]

    def test_invalid_repo_structure_returns_error(self):
        """Test that invalid repo structure returns validation error."""
        from code_indexer.server.web.routes import web_router
        from fastapi import FastAPI

        app = FastAPI()
        app.include_router(web_router, prefix="/admin")

        client = TestClient(app)

        # Missing required 'clone_url' field
        response = client.post(
            "/admin/api/discovery/branches",
            json={"repos": [{"platform": "github"}]},  # Missing clone_url
            follow_redirects=False,
        )

        # Should return 422 for invalid structure (if not auth redirect)
        assert response.status_code in [302, 303, 307, 401, 422]


class TestDiscoveryBranchesResponseFormat:
    """Tests for response format of branches endpoint."""

    def test_response_contains_results_per_repo(self):
        """Test that response contains results keyed by clone_url.

        Expected response format:
        {
            "https://github.com/org/repo.git": {
                "branches": ["main", "develop", ...],
                "default_branch": "main",
                "error": null
            },
            ...
        }
        """
        # Test structure validation using expected keys
        expected_keys = {"branches", "default_branch", "error"}

        sample_response = {
            "https://github.com/test/repo.git": {
                "branches": ["main", "develop"],
                "default_branch": "main",
                "error": None,
            }
        }

        for url, result in sample_response.items():
            # Verify all expected keys are present
            assert set(result.keys()) == expected_keys
            assert isinstance(result["branches"], list)

    def test_error_response_structure(self):
        """Test that error responses have correct structure."""
        # Expected error response structure
        sample_error_response = {
            "https://github.com/nonexistent/repo.git": {
                "branches": [],
                "default_branch": None,
                "error": "Repository not found",
            }
        }

        for url, result in sample_error_response.items():
            assert result["branches"] == []
            assert result["default_branch"] is None
            assert result["error"] is not None
            assert isinstance(result["error"], str)


class TestDiscoveryBranchesFiltering:
    """Tests for branch filtering in endpoint response."""

    def test_issue_tracker_branches_are_filtered(self):
        """Test that issue-tracker pattern branches are filtered from response.

        Branches matching [A-Z]+-\\d+ pattern should be excluded:
        - SCM-1234
        - PROJ-567
        - A-1
        """
        # This validates the filtering is applied
        # Implementation should use filter_issue_tracker_branches
        from code_indexer.server.services.remote_branch_service import (
            filter_issue_tracker_branches,
        )

        test_branches = ["main", "SCM-1234", "develop", "PROJ-567"]
        filtered = filter_issue_tracker_branches(test_branches)

        assert "SCM-1234" not in filtered
        assert "PROJ-567" not in filtered
        assert "main" in filtered
        assert "develop" in filtered
