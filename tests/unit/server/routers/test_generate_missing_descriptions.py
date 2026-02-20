"""
Unit tests for POST /admin/diagnostics/generate-missing-descriptions endpoint.

Story #233: Generate Missing Repo Descriptions Endpoint with Diagnostics UI

Acceptance Criteria tested:
- AC1: Admin triggers description generation via POST endpoint
- AC3: Description generation is idempotent (skips repos that already have descriptions)
- AC4: Individual failures don't block other repos
- AC5: Generated descriptions are readable via existing GET endpoint
"""

import pytest
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

from fastapi.testclient import TestClient

from code_indexer.server.app import app
from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.auth.dependencies import (
    get_current_user_hybrid,
    get_current_admin_user_hybrid,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def admin_user():
    """Return an admin User for dependency injection."""
    return User(
        username="testadmin",
        password_hash="hashed_password",
        role=UserRole.ADMIN,
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )


@pytest.fixture
def authenticated_admin_client(admin_user):
    """Create test client with admin auth mocked."""
    app.dependency_overrides[get_current_user_hybrid] = lambda: admin_user
    app.dependency_overrides[get_current_admin_user_hybrid] = lambda: admin_user
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def temp_golden_repos_dir():
    """Create a temporary golden-repos directory with cidx-meta subdir."""
    with tempfile.TemporaryDirectory() as tmpdir:
        cidx_meta_dir = Path(tmpdir) / "cidx-meta"
        cidx_meta_dir.mkdir(parents=True)
        yield tmpdir


@pytest.fixture
def mock_golden_repo_manager_factory():
    """Factory for creating a mock golden repo manager with configurable repos."""

    def _make_manager(repos):
        """
        Args:
            repos: list of dicts with keys: alias, repo_url, clone_path
        """
        mock = MagicMock()
        mock.list_golden_repos.return_value = repos
        return mock

    return _make_manager


def _set_app_state(golden_repos_dir, golden_repo_manager):
    """Helper: set app state for tests."""
    app.state.golden_repos_dir = golden_repos_dir
    app.state.golden_repo_manager = golden_repo_manager


def _clear_app_state():
    """Helper: clear app state after tests."""
    for attr in ("golden_repos_dir", "golden_repo_manager"):
        if hasattr(app.state, attr):
            delattr(app.state, attr)


# ---------------------------------------------------------------------------
# AC1: Endpoint exists and returns correct response shape
# ---------------------------------------------------------------------------


class TestEndpointExists:
    """AC1: POST /admin/diagnostics/generate-missing-descriptions exists."""

    def test_endpoint_response_shape_with_empty_repos(
        self, authenticated_admin_client, temp_golden_repos_dir
    ):
        """AC1: Endpoint returns 200 with correct JSON shape even with no repos."""
        mock_manager = MagicMock()
        mock_manager.list_golden_repos.return_value = []
        _set_app_state(temp_golden_repos_dir, mock_manager)

        try:
            response = authenticated_admin_client.post(
                "/admin/diagnostics/generate-missing-descriptions"
            )
            assert response.status_code == 200
            data = response.json()
            assert isinstance(data.get("repos_queued"), int)
            assert isinstance(data.get("repos_with_descriptions"), int)
            assert isinstance(data.get("total_repos"), int)
            assert data["total_repos"] == 0
            assert data["repos_queued"] == 0
            assert data["repos_with_descriptions"] == 0
        finally:
            _clear_app_state()

    def test_endpoint_counts_repos_needing_descriptions(
        self,
        authenticated_admin_client,
        temp_golden_repos_dir,
        mock_golden_repo_manager_factory,
    ):
        """AC1: Endpoint counts repos that need descriptions (no .md file present)."""
        repos = [
            {
                "alias": "repo-a",
                "repo_url": "https://github.com/org/repo-a.git",
                "clone_path": str(Path(temp_golden_repos_dir) / "repo-a"),
            },
            {
                "alias": "repo-b",
                "repo_url": "https://github.com/org/repo-b.git",
                "clone_path": str(Path(temp_golden_repos_dir) / "repo-b"),
            },
        ]
        mock_manager = mock_golden_repo_manager_factory(repos)
        _set_app_state(temp_golden_repos_dir, mock_manager)

        try:
            with patch(
                "code_indexer.server.routers.diagnostics.get_claude_cli_manager"
            ) as mock_get_cli:
                mock_cli = MagicMock()
                mock_cli.check_cli_available.return_value = True
                mock_get_cli.return_value = mock_cli

                response = authenticated_admin_client.post(
                    "/admin/diagnostics/generate-missing-descriptions"
                )
                data = response.json()
                assert data["total_repos"] == 2
                assert data["repos_queued"] == 2
                assert data["repos_with_descriptions"] == 0
        finally:
            _clear_app_state()


# ---------------------------------------------------------------------------
# AC3: Idempotent - skips repos that already have descriptions
# ---------------------------------------------------------------------------


class TestIdempotency:
    """AC3: Description generation is idempotent."""

    def test_skips_repo_with_existing_description(
        self,
        authenticated_admin_client,
        temp_golden_repos_dir,
        mock_golden_repo_manager_factory,
    ):
        """AC3: Repo with existing .md file is NOT queued for generation."""
        cidx_meta_dir = Path(temp_golden_repos_dir) / "cidx-meta"
        (cidx_meta_dir / "existing-repo.md").write_text(
            "# existing-repo\nAlready has a description.\n"
        )

        repos = [
            {
                "alias": "existing-repo",
                "repo_url": "https://github.com/org/existing-repo.git",
                "clone_path": str(Path(temp_golden_repos_dir) / "existing-repo"),
            },
        ]
        mock_manager = mock_golden_repo_manager_factory(repos)
        _set_app_state(temp_golden_repos_dir, mock_manager)

        try:
            response = authenticated_admin_client.post(
                "/admin/diagnostics/generate-missing-descriptions"
            )
            data = response.json()
            assert data["total_repos"] == 1
            assert data["repos_queued"] == 0
            assert data["repos_with_descriptions"] == 1
        finally:
            _clear_app_state()

    def test_only_queues_repos_missing_descriptions(
        self,
        authenticated_admin_client,
        temp_golden_repos_dir,
        mock_golden_repo_manager_factory,
    ):
        """AC3: Mixed set - only repos without .md files are queued."""
        cidx_meta_dir = Path(temp_golden_repos_dir) / "cidx-meta"
        (cidx_meta_dir / "has-description.md").write_text(
            "# has-description\nAlready described.\n"
        )

        repos = [
            {
                "alias": "has-description",
                "repo_url": "https://github.com/org/has-description.git",
                "clone_path": str(Path(temp_golden_repos_dir) / "has-description"),
            },
            {
                "alias": "needs-description",
                "repo_url": "https://github.com/org/needs-description.git",
                "clone_path": str(Path(temp_golden_repos_dir) / "needs-description"),
            },
        ]
        mock_manager = mock_golden_repo_manager_factory(repos)
        _set_app_state(temp_golden_repos_dir, mock_manager)

        try:
            with patch(
                "code_indexer.server.routers.diagnostics.get_claude_cli_manager"
            ) as mock_get_cli:
                mock_cli = MagicMock()
                mock_cli.check_cli_available.return_value = True
                mock_get_cli.return_value = mock_cli

                response = authenticated_admin_client.post(
                    "/admin/diagnostics/generate-missing-descriptions"
                )
                data = response.json()
                assert data["total_repos"] == 2
                assert data["repos_queued"] == 1
                assert data["repos_with_descriptions"] == 1
        finally:
            _clear_app_state()

    def test_cidx_meta_itself_is_excluded(
        self,
        authenticated_admin_client,
        temp_golden_repos_dir,
        mock_golden_repo_manager_factory,
    ):
        """AC3: cidx-meta repo is excluded from description generation."""
        repos = [
            {
                "alias": "cidx-meta",
                "repo_url": "local://cidx-meta",
                "clone_path": str(Path(temp_golden_repos_dir) / "cidx-meta"),
            },
            {
                "alias": "real-repo",
                "repo_url": "https://github.com/org/real-repo.git",
                "clone_path": str(Path(temp_golden_repos_dir) / "real-repo"),
            },
        ]
        mock_manager = mock_golden_repo_manager_factory(repos)
        _set_app_state(temp_golden_repos_dir, mock_manager)

        try:
            with patch(
                "code_indexer.server.routers.diagnostics.get_claude_cli_manager"
            ) as mock_get_cli:
                mock_cli = MagicMock()
                mock_cli.check_cli_available.return_value = True
                mock_get_cli.return_value = mock_cli

                response = authenticated_admin_client.post(
                    "/admin/diagnostics/generate-missing-descriptions"
                )
                data = response.json()
                # cidx-meta is excluded; only real-repo counts
                assert data["total_repos"] == 1
                assert data["repos_queued"] == 1
        finally:
            _clear_app_state()


# ---------------------------------------------------------------------------
# AC4: Individual failures don't block other repos
# ---------------------------------------------------------------------------


class TestFailureIsolation:
    """AC4: Individual failures don't block other repos."""

    def test_submit_work_called_for_each_missing_repo(
        self,
        authenticated_admin_client,
        temp_golden_repos_dir,
        mock_golden_repo_manager_factory,
    ):
        """AC4: submit_work is called for each repo missing a description."""
        repos = [
            {
                "alias": f"repo-{i}",
                "repo_url": f"https://github.com/org/repo-{i}.git",
                "clone_path": str(Path(temp_golden_repos_dir) / f"repo-{i}"),
            }
            for i in range(1, 4)
        ]
        mock_manager = mock_golden_repo_manager_factory(repos)
        _set_app_state(temp_golden_repos_dir, mock_manager)

        try:
            with patch(
                "code_indexer.server.routers.diagnostics.get_claude_cli_manager"
            ) as mock_get_cli:
                mock_cli = MagicMock()
                mock_cli.check_cli_available.return_value = True
                mock_get_cli.return_value = mock_cli

                response = authenticated_admin_client.post(
                    "/admin/diagnostics/generate-missing-descriptions"
                )
                assert response.status_code == 200
                data = response.json()
                assert data["repos_queued"] == 3
                assert mock_cli.submit_work.call_count == 3
        finally:
            _clear_app_state()

    def test_failure_in_one_repo_does_not_block_others(
        self,
        authenticated_admin_client,
        temp_golden_repos_dir,
        mock_golden_repo_manager_factory,
    ):
        """AC4: Even if submit_work raises for one repo, others are still queued."""
        repos = [
            {
                "alias": "good-repo-1",
                "repo_url": "https://github.com/org/good-repo-1.git",
                "clone_path": str(Path(temp_golden_repos_dir) / "good-repo-1"),
            },
            {
                "alias": "bad-repo",
                "repo_url": "https://github.com/org/bad-repo.git",
                "clone_path": str(Path(temp_golden_repos_dir) / "bad-repo"),
            },
            {
                "alias": "good-repo-2",
                "repo_url": "https://github.com/org/good-repo-2.git",
                "clone_path": str(Path(temp_golden_repos_dir) / "good-repo-2"),
            },
        ]
        mock_manager = mock_golden_repo_manager_factory(repos)
        _set_app_state(temp_golden_repos_dir, mock_manager)

        try:
            with patch(
                "code_indexer.server.routers.diagnostics.get_claude_cli_manager"
            ) as mock_get_cli:
                mock_cli = MagicMock()
                mock_cli.check_cli_available.return_value = True

                call_count = [0]

                def side_effect_submit(repo_path, callback):
                    call_count[0] += 1
                    if call_count[0] == 2:
                        raise RuntimeError("Simulated failure for bad-repo")

                mock_cli.submit_work.side_effect = side_effect_submit
                mock_get_cli.return_value = mock_cli

                response = authenticated_admin_client.post(
                    "/admin/diagnostics/generate-missing-descriptions"
                )
                # Must return 200, not 500 - failures are isolated
                assert response.status_code == 200
                # submit_work called 3 times despite one failure
                assert mock_cli.submit_work.call_count == 3
        finally:
            _clear_app_state()


# ---------------------------------------------------------------------------
# AC5: Generated descriptions are readable via existing GET endpoint
# ---------------------------------------------------------------------------


class TestDescriptionReadability:
    """AC5: Generated descriptions are readable via existing GET endpoint."""

    def test_existing_description_readable_after_endpoint_call(
        self,
        authenticated_admin_client,
        temp_golden_repos_dir,
        mock_golden_repo_manager_factory,
    ):
        """AC5: After queuing, repos with .md files remain readable via GET."""
        cidx_meta_dir = Path(temp_golden_repos_dir) / "cidx-meta"
        (cidx_meta_dir / "readable-repo.md").write_text(
            "---\nname: readable-repo\n---\n\n# readable-repo\n\nThis is readable.\n"
        )

        repos = [
            {
                "alias": "readable-repo",
                "repo_url": "https://github.com/org/readable-repo.git",
                "clone_path": str(Path(temp_golden_repos_dir) / "readable-repo"),
            },
        ]
        mock_manager = mock_golden_repo_manager_factory(repos)
        _set_app_state(temp_golden_repos_dir, mock_manager)

        try:
            gen_response = authenticated_admin_client.post(
                "/admin/diagnostics/generate-missing-descriptions"
            )
            assert gen_response.status_code == 200
            gen_data = gen_response.json()
            assert gen_data["repos_with_descriptions"] == 1
            assert gen_data["repos_queued"] == 0

            # Description must still be readable via the GET endpoint
            get_response = authenticated_admin_client.get(
                "/api/repositories/readable-repo/description"
            )
            assert get_response.status_code == 200
            desc_data = get_response.json()
            assert "# readable-repo" in desc_data["description"]
            assert "This is readable." in desc_data["description"]
        finally:
            _clear_app_state()


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


class TestAuthentication:
    """Tests for auth requirements on the new endpoint."""

    def test_unauthenticated_request_returns_401(self):
        """Unauthenticated requests should return 401."""
        app.dependency_overrides.clear()
        client = TestClient(app)

        response = client.post(
            "/admin/diagnostics/generate-missing-descriptions"
        )
        assert response.status_code == 401

    def test_no_app_state_returns_500(self, authenticated_admin_client):
        """When golden_repos_dir is not set on app state, returns 500."""
        original_dir = getattr(app.state, "golden_repos_dir", None)
        original_manager = getattr(app.state, "golden_repo_manager", None)
        _clear_app_state()

        try:
            response = authenticated_admin_client.post(
                "/admin/diagnostics/generate-missing-descriptions"
            )
            assert response.status_code == 500
        finally:
            if original_dir is not None:
                app.state.golden_repos_dir = original_dir
            if original_manager is not None:
                app.state.golden_repo_manager = original_manager
