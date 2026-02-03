"""
Unit tests for CI/CD REST API route registration.

Story #745: CI/CD Monitoring REST Endpoints

TDD RED PHASE: Tests verify that all 12 CI/CD routes are registered with correct
paths and HTTP methods. Tests should FAIL initially until the router is implemented.
"""

import pytest
from fastapi import FastAPI


class TestCICDRoutesRegistration:
    """Test that all 12 CI/CD routes are properly registered."""

    @pytest.fixture
    def app_with_cicd_router(self):
        """Create FastAPI app with CI/CD router for testing."""
        from code_indexer.server.routes.cicd import router as cicd_router

        app = FastAPI()
        app.include_router(cicd_router)
        return app

    def test_cicd_router_can_be_imported(self):
        """Test that CI/CD router module can be imported."""
        from code_indexer.server.routes import cicd

        assert hasattr(cicd, "router")

    def test_github_list_runs_route_exists(self, app_with_cicd_router):
        """Test GitHub list runs route is registered (endpoint 1/12)."""
        routes = [route.path for route in app_with_cicd_router.routes]
        assert "/api/cicd/github/{owner}/{repo}/runs" in routes

    def test_github_get_run_route_exists(self, app_with_cicd_router):
        """Test GitHub get run route is registered (endpoint 2/12)."""
        routes = [route.path for route in app_with_cicd_router.routes]
        assert "/api/cicd/github/{owner}/{repo}/runs/{run_id}" in routes

    def test_github_search_logs_route_exists(self, app_with_cicd_router):
        """Test GitHub search logs route is registered (endpoint 3/12)."""
        routes = [route.path for route in app_with_cicd_router.routes]
        assert "/api/cicd/github/{owner}/{repo}/runs/{run_id}/logs" in routes

    def test_github_get_job_logs_route_exists(self, app_with_cicd_router):
        """Test GitHub get job logs route is registered (endpoint 4/12)."""
        routes = [route.path for route in app_with_cicd_router.routes]
        assert "/api/cicd/github/{owner}/{repo}/jobs/{job_id}/logs" in routes

    def test_github_retry_run_route_exists(self, app_with_cicd_router):
        """Test GitHub retry run route is registered (endpoint 5/12)."""
        routes = [route.path for route in app_with_cicd_router.routes]
        assert "/api/cicd/github/{owner}/{repo}/runs/{run_id}/retry" in routes

    def test_github_cancel_run_route_exists(self, app_with_cicd_router):
        """Test GitHub cancel run route is registered (endpoint 6/12)."""
        routes = [route.path for route in app_with_cicd_router.routes]
        assert "/api/cicd/github/{owner}/{repo}/runs/{run_id}/cancel" in routes

    def test_gitlab_list_pipelines_route_exists(self, app_with_cicd_router):
        """Test GitLab list pipelines route is registered (endpoint 7/12)."""
        routes = [route.path for route in app_with_cicd_router.routes]
        assert "/api/cicd/gitlab/{project_id}/pipelines" in routes

    def test_gitlab_get_pipeline_route_exists(self, app_with_cicd_router):
        """Test GitLab get pipeline route is registered (endpoint 8/12)."""
        routes = [route.path for route in app_with_cicd_router.routes]
        assert "/api/cicd/gitlab/{project_id}/pipelines/{pipeline_id}" in routes

    def test_gitlab_search_logs_route_exists(self, app_with_cicd_router):
        """Test GitLab search logs route is registered (endpoint 9/12)."""
        routes = [route.path for route in app_with_cicd_router.routes]
        assert "/api/cicd/gitlab/{project_id}/pipelines/{pipeline_id}/logs" in routes

    def test_gitlab_get_job_logs_route_exists(self, app_with_cicd_router):
        """Test GitLab get job logs route is registered (endpoint 10/12)."""
        routes = [route.path for route in app_with_cicd_router.routes]
        assert "/api/cicd/gitlab/{project_id}/jobs/{job_id}/logs" in routes

    def test_gitlab_retry_pipeline_route_exists(self, app_with_cicd_router):
        """Test GitLab retry pipeline route is registered (endpoint 11/12)."""
        routes = [route.path for route in app_with_cicd_router.routes]
        assert "/api/cicd/gitlab/{project_id}/pipelines/{pipeline_id}/retry" in routes

    def test_gitlab_cancel_pipeline_route_exists(self, app_with_cicd_router):
        """Test GitLab cancel pipeline route is registered (endpoint 12/12)."""
        routes = [route.path for route in app_with_cicd_router.routes]
        assert "/api/cicd/gitlab/{project_id}/pipelines/{pipeline_id}/cancel" in routes

    def test_all_twelve_routes_registered(self, app_with_cicd_router):
        """Test that exactly 12 CI/CD routes are registered."""
        routes = [route.path for route in app_with_cicd_router.routes]
        cicd_routes = [r for r in routes if r.startswith("/api/cicd/")]
        assert (
            len(cicd_routes) == 12
        ), f"Expected 12 routes, found {len(cicd_routes)}: {cicd_routes}"


class TestCICDRoutesHTTPMethods:
    """Test that routes use correct HTTP methods."""

    @pytest.fixture
    def app_with_cicd_router(self):
        """Create FastAPI app with CI/CD router for testing."""
        from code_indexer.server.routes.cicd import router as cicd_router

        app = FastAPI()
        app.include_router(cicd_router)
        return app

    def test_github_list_runs_is_get(self, app_with_cicd_router):
        """Test GitHub list runs uses GET method."""
        for route in app_with_cicd_router.routes:
            if route.path == "/api/cicd/github/{owner}/{repo}/runs":
                assert "GET" in route.methods

    def test_github_get_run_is_get(self, app_with_cicd_router):
        """Test GitHub get run uses GET method."""
        for route in app_with_cicd_router.routes:
            if route.path == "/api/cicd/github/{owner}/{repo}/runs/{run_id}":
                assert "GET" in route.methods

    def test_github_search_logs_is_get(self, app_with_cicd_router):
        """Test GitHub search logs uses GET method."""
        for route in app_with_cicd_router.routes:
            if route.path == "/api/cicd/github/{owner}/{repo}/runs/{run_id}/logs":
                assert "GET" in route.methods

    def test_github_get_job_logs_is_get(self, app_with_cicd_router):
        """Test GitHub get job logs uses GET method."""
        for route in app_with_cicd_router.routes:
            if route.path == "/api/cicd/github/{owner}/{repo}/jobs/{job_id}/logs":
                assert "GET" in route.methods

    def test_github_retry_run_is_post(self, app_with_cicd_router):
        """Test GitHub retry run uses POST method."""
        for route in app_with_cicd_router.routes:
            if route.path == "/api/cicd/github/{owner}/{repo}/runs/{run_id}/retry":
                assert "POST" in route.methods

    def test_github_cancel_run_is_post(self, app_with_cicd_router):
        """Test GitHub cancel run uses POST method."""
        for route in app_with_cicd_router.routes:
            if route.path == "/api/cicd/github/{owner}/{repo}/runs/{run_id}/cancel":
                assert "POST" in route.methods

    def test_gitlab_list_pipelines_is_get(self, app_with_cicd_router):
        """Test GitLab list pipelines uses GET method."""
        for route in app_with_cicd_router.routes:
            if route.path == "/api/cicd/gitlab/{project_id}/pipelines":
                assert "GET" in route.methods

    def test_gitlab_get_pipeline_is_get(self, app_with_cicd_router):
        """Test GitLab get pipeline uses GET method."""
        for route in app_with_cicd_router.routes:
            if route.path == "/api/cicd/gitlab/{project_id}/pipelines/{pipeline_id}":
                assert "GET" in route.methods

    def test_gitlab_search_logs_is_get(self, app_with_cicd_router):
        """Test GitLab search logs uses GET method."""
        for route in app_with_cicd_router.routes:
            if (
                route.path
                == "/api/cicd/gitlab/{project_id}/pipelines/{pipeline_id}/logs"
            ):
                assert "GET" in route.methods

    def test_gitlab_get_job_logs_is_get(self, app_with_cicd_router):
        """Test GitLab get job logs uses GET method."""
        for route in app_with_cicd_router.routes:
            if route.path == "/api/cicd/gitlab/{project_id}/jobs/{job_id}/logs":
                assert "GET" in route.methods

    def test_gitlab_retry_pipeline_is_post(self, app_with_cicd_router):
        """Test GitLab retry pipeline uses POST method."""
        for route in app_with_cicd_router.routes:
            if (
                route.path
                == "/api/cicd/gitlab/{project_id}/pipelines/{pipeline_id}/retry"
            ):
                assert "POST" in route.methods

    def test_gitlab_cancel_pipeline_is_post(self, app_with_cicd_router):
        """Test GitLab cancel pipeline uses POST method."""
        for route in app_with_cicd_router.routes:
            if (
                route.path
                == "/api/cicd/gitlab/{project_id}/pipelines/{pipeline_id}/cancel"
            ):
                assert "POST" in route.methods
