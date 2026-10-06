"""Bug #1991: REST git/cat (twin of MCP git_file_at_revision) returns the
text of non-UTF-8 files, decoded like indexing decodes them.

Real temp git repository and the real route; only authentication and the
alias -> repository path lookup are replaced.
"""

from __future__ import annotations

import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator
from unittest.mock import Mock, patch

import pytest
from fastapi.testclient import TestClient

from code_indexer.server.auth.user_manager import User, UserRole

LATIN1_FILE = "Legacy.cs"
LATIN1_BYTES = "/// Caf\xe9 image generator\nclass G { }\n".encode("latin-1")
UTF8_FILE = "modern.py"
UTF8_BYTES = "# café\r\ndef f():\r\n    return 1\r\n".encode("utf-8")

ADMIN = User(
    username="admin",
    password_hash="$2b$12$hash",
    role=UserRole.ADMIN,
    created_at=datetime.now(timezone.utc),
)


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, capture_output=True, check=True)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    _git(tmp_path, "init")
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "Test")
    _git(tmp_path, "config", "core.autocrlf", "false")
    (tmp_path / LATIN1_FILE).write_bytes(LATIN1_BYTES)
    (tmp_path / UTF8_FILE).write_bytes(UTF8_BYTES)
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-m", "init")
    return tmp_path


@pytest.fixture
def client(tmp_path_factory: pytest.TempPathFactory) -> Iterator[TestClient]:
    from code_indexer.server.app import create_app
    from code_indexer.server.auth.dependencies import get_current_user
    from tests.unit.server.routers.inline_routes_test_helpers import (
        _access_service_admin,
    )

    app = create_app()
    app.dependency_overrides[get_current_user] = lambda: ADMIN
    # The caller is an admin of a real access service, so the activated-repo
    # guard passes; these tests pin decoding, not access control.
    groups_db = tmp_path_factory.mktemp("access") / "groups.db"
    with _access_service_admin(groups_db, ADMIN.username):
        yield TestClient(app)


def _cat(client: TestClient, repo: Path, path: str) -> str:
    repo_lookup = Mock()
    repo_lookup.get_activated_repo_path.return_value = str(repo)
    with patch(
        "code_indexer.server.routers.git._get_activated_repo_manager",
        return_value=repo_lookup,
    ):
        response = client.get(
            "/api/v1/repos/example-repo/git/cat", params={"path": path}
        )
    assert response.status_code == 200, response.text
    content: str = response.json()["content"]
    return content


def test_rest_git_cat_returns_latin1_text(client: TestClient, repo: Path) -> None:
    assert _cat(client, repo, LATIN1_FILE) == LATIN1_BYTES.decode("latin-1")


def test_rest_git_cat_utf8_unchanged(client: TestClient, repo: Path) -> None:
    assert _cat(client, repo, UTF8_FILE) == UTF8_BYTES.decode("utf-8")
