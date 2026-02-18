"""
Unit tests for Repository Description REST endpoint.

Tests the GET /api/repositories/{repo_alias}/description endpoint that provides
cidx-meta markdown description content with frontmatter stripped.

Story #218: Golden Repo cidx-meta Description Display
"""

import pytest
import tempfile
from pathlib import Path
from datetime import datetime, timezone

from fastapi.testclient import TestClient

from code_indexer.server.app import app
from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.auth.dependencies import get_current_user_hybrid


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
def authenticated_client(admin_user):
    """Create test client with mocked authentication."""
    app.dependency_overrides[get_current_user_hybrid] = lambda: admin_user
    yield TestClient(app)
    app.dependency_overrides.clear()


@pytest.fixture
def temp_golden_repos_dir():
    """Create a temporary directory that mimics ~/.cidx-server/data/golden-repos."""
    with tempfile.TemporaryDirectory() as tmpdir:
        cidx_meta_dir = Path(tmpdir) / "cidx-meta"
        cidx_meta_dir.mkdir(parents=True)
        yield tmpdir


SAMPLE_CIDX_META_WITH_FRONTMATTER = """\
---
name: code-indexer-python
url: https://github.com/jsbattig/code-indexer.git
technologies:
  - Python
  - FastAPI
purpose: cli-tool
last_analyzed: 2025-12-11T13:44:09.400876+00:00
---

# code-indexer-python

CIDX (Code Indexer) is an AI-powered semantic code search tool.

## Features

- semantic code search using AI embeddings
- full-text search with fuzzy matching
"""

SAMPLE_CIDX_META_NO_FRONTMATTER = """\
# my-repo

Simple repository with no frontmatter.

## Description

Just a plain markdown file.
"""

SAMPLE_CIDX_META_ONLY_FRONTMATTER = """\
---
name: empty-repo
purpose: testing
---
"""


# ---------------------------------------------------------------------------
# AC1: Description displays when cidx-meta file exists
# ---------------------------------------------------------------------------


def test_description_endpoint_file_found(authenticated_client, temp_golden_repos_dir):
    """AC1: Returns 200 with markdown body when cidx-meta file exists."""
    # Arrange: write the cidx-meta file
    meta_path = Path(temp_golden_repos_dir) / "cidx-meta" / "code-indexer-python.md"
    meta_path.write_text(SAMPLE_CIDX_META_WITH_FRONTMATTER, encoding="utf-8")

    # Patch app state so endpoint finds the directory
    app.state.golden_repos_dir = temp_golden_repos_dir

    try:
        # Act
        response = authenticated_client.get(
            "/api/repositories/code-indexer-python/description"
        )

        # Assert
        assert response.status_code == 200
        data = response.json()
        assert "description" in data
        # Body must contain the markdown content
        assert "# code-indexer-python" in data["description"]
        assert "CIDX (Code Indexer)" in data["description"]
        assert "## Features" in data["description"]
    finally:
        # Clean up app state
        if hasattr(app.state, "golden_repos_dir"):
            del app.state.golden_repos_dir


# ---------------------------------------------------------------------------
# AC1: Frontmatter stripping
# ---------------------------------------------------------------------------


def test_description_endpoint_frontmatter_stripping(
    authenticated_client, temp_golden_repos_dir
):
    """AC1: YAML frontmatter between --- delimiters is NOT returned in the body."""
    # Arrange
    meta_path = Path(temp_golden_repos_dir) / "cidx-meta" / "code-indexer-python.md"
    meta_path.write_text(SAMPLE_CIDX_META_WITH_FRONTMATTER, encoding="utf-8")

    app.state.golden_repos_dir = temp_golden_repos_dir

    try:
        # Act
        response = authenticated_client.get(
            "/api/repositories/code-indexer-python/description"
        )

        # Assert
        assert response.status_code == 200
        data = response.json()
        description = data["description"]

        # Frontmatter keys must NOT appear in body
        assert "name: code-indexer-python" not in description
        assert "url: https://github.com" not in description
        assert "technologies:" not in description
        assert "purpose: cli-tool" not in description
        assert "last_analyzed:" not in description
        # The --- delimiters themselves must NOT appear
        assert description.strip().startswith("#") or description.strip() == ""
    finally:
        if hasattr(app.state, "golden_repos_dir"):
            del app.state.golden_repos_dir


def test_description_endpoint_file_no_frontmatter(
    authenticated_client, temp_golden_repos_dir
):
    """AC1: File with no frontmatter returns full content unchanged."""
    # Arrange
    meta_path = Path(temp_golden_repos_dir) / "cidx-meta" / "my-repo.md"
    meta_path.write_text(SAMPLE_CIDX_META_NO_FRONTMATTER, encoding="utf-8")

    app.state.golden_repos_dir = temp_golden_repos_dir

    try:
        # Act
        response = authenticated_client.get("/api/repositories/my-repo/description")

        # Assert
        assert response.status_code == 200
        data = response.json()
        description = data["description"]
        assert "# my-repo" in description
        assert "Simple repository with no frontmatter." in description
        assert "Just a plain markdown file." in description
    finally:
        if hasattr(app.state, "golden_repos_dir"):
            del app.state.golden_repos_dir


def test_description_endpoint_empty_body_after_frontmatter(
    authenticated_client, temp_golden_repos_dir
):
    """AC1: File that has ONLY frontmatter returns empty string body (no error)."""
    # Arrange: write file with ONLY frontmatter, no body
    meta_path = Path(temp_golden_repos_dir) / "cidx-meta" / "empty-repo.md"
    meta_path.write_text(SAMPLE_CIDX_META_ONLY_FRONTMATTER, encoding="utf-8")

    app.state.golden_repos_dir = temp_golden_repos_dir

    try:
        # Act
        response = authenticated_client.get("/api/repositories/empty-repo/description")

        # Assert: endpoint returns 200 with empty or whitespace-only description
        assert response.status_code == 200
        data = response.json()
        assert "description" in data
        # Body after stripping frontmatter must be empty or whitespace
        assert data["description"].strip() == ""
    finally:
        if hasattr(app.state, "golden_repos_dir"):
            del app.state.golden_repos_dir


# ---------------------------------------------------------------------------
# AC2: Error displays when cidx-meta file is missing
# ---------------------------------------------------------------------------


def test_description_endpoint_file_not_found(
    authenticated_client, temp_golden_repos_dir
):
    """AC2: Returns 404 with error message when cidx-meta file does not exist."""
    # Arrange: do NOT create the file - golden_repos_dir exists but has no entry
    app.state.golden_repos_dir = temp_golden_repos_dir

    try:
        # Act
        response = authenticated_client.get("/api/repositories/new-repo/description")

        # Assert
        assert response.status_code == 404
        data = response.json()
        assert "detail" in data
        # Error message should be informative
        assert "new-repo" in data["detail"] or "cidx-meta" in data["detail"] or "not found" in data["detail"].lower()
    finally:
        if hasattr(app.state, "golden_repos_dir"):
            del app.state.golden_repos_dir


def test_description_endpoint_no_golden_repos_dir_returns_404(authenticated_client):
    """AC2: When golden_repos_dir is not set, returns 404 (no fallback)."""
    # Arrange: ensure golden_repos_dir is NOT set on app state
    # (clear any previously set value)
    original = getattr(app.state, "golden_repos_dir", None)
    if hasattr(app.state, "golden_repos_dir"):
        del app.state.golden_repos_dir

    try:
        # Act
        response = authenticated_client.get(
            "/api/repositories/some-repo/description"
        )

        # Assert: must return 404, not 500 or redirect
        assert response.status_code == 404
    finally:
        if original is not None:
            app.state.golden_repos_dir = original


# ---------------------------------------------------------------------------
# Authentication
# ---------------------------------------------------------------------------


def test_description_endpoint_unauthenticated():
    """Returns 401 for unauthenticated requests."""
    # Clear any dependency overrides to ensure no auth bypass
    app.dependency_overrides.clear()
    client = TestClient(app)

    response = client.get("/api/repositories/code-indexer-python/description")

    assert response.status_code == 401


# ---------------------------------------------------------------------------
# Response shape contract
# ---------------------------------------------------------------------------


def test_description_endpoint_response_has_correct_shape(
    authenticated_client, temp_golden_repos_dir
):
    """Response JSON has required fields: description (str) and repo_alias (str)."""
    # Arrange
    meta_path = Path(temp_golden_repos_dir) / "cidx-meta" / "code-indexer-python.md"
    meta_path.write_text(SAMPLE_CIDX_META_WITH_FRONTMATTER, encoding="utf-8")

    app.state.golden_repos_dir = temp_golden_repos_dir

    try:
        # Act
        response = authenticated_client.get(
            "/api/repositories/code-indexer-python/description"
        )

        # Assert shape
        assert response.status_code == 200
        data = response.json()
        assert isinstance(data.get("description"), str)
        assert isinstance(data.get("repo_alias"), str)
        assert data["repo_alias"] == "code-indexer-python"
    finally:
        if hasattr(app.state, "golden_repos_dir"):
            del app.state.golden_repos_dir


# ---------------------------------------------------------------------------
# Security: Path traversal prevention (Story #218 security fix)
# ---------------------------------------------------------------------------


def test_description_path_traversal_returns_404(
    authenticated_client, temp_golden_repos_dir
):
    """Security: Crafted alias with path traversal sequences returns 404 not a file read.

    A repo_alias like '../../etc/passwd' must NOT escape the cidx-meta directory.
    The endpoint must return 404, not 500 or file contents.
    """
    # Arrange: golden_repos_dir exists and is configured
    app.state.golden_repos_dir = temp_golden_repos_dir

    try:
        # Act: send a traversal alias via URL encoding handled by FastAPI
        response = authenticated_client.get(
            "/api/repositories/..%2F..%2Fetc%2Fpasswd/description"
        )

        # Assert: must be 404 (traversal blocked), not 200 or 500
        assert response.status_code == 404
        data = response.json()
        assert "detail" in data
    finally:
        if hasattr(app.state, "golden_repos_dir"):
            del app.state.golden_repos_dir
