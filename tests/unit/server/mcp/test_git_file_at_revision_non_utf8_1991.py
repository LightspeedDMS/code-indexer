"""Bug #1991: git_file_at_revision returns the text of non-UTF-8 files.

Real temp git repository; the MCP handler runs for real with only its
repository-path resolution replaced (alias -> the temp repo).
"""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Dict
from unittest.mock import patch

import pytest

from code_indexer.server.auth.user_manager import User, UserRole

LATIN1_FILE = "Legacy.cs"
LATIN1_BYTES = "/// Caf\xe9 image generator\nclass G { }\n".encode("latin-1")
UTF8_FILE = "modern.py"
UTF8_BYTES = "# café\r\ndef f():\r\n    return 1\r\n".encode("utf-8")

USER = User(
    username="admin",
    password_hash="$2b$12$hash",
    role=UserRole.ADMIN,
    created_at=datetime.now(timezone.utc),
)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout


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


def test_service_decodes_latin1_and_keeps_utf8_unchanged(repo: Path) -> None:
    from code_indexer.global_repos.git_operations import GitOperationsService

    service = GitOperationsService(repo)

    latin1 = service.get_file_at_revision(path=LATIN1_FILE, revision="HEAD")
    assert latin1.content == "/// Caf\xe9 image generator\nclass G { }\n"
    # size_bytes is the blob's real byte count, not its UTF-8 re-encoding.
    assert latin1.size_bytes == len(LATIN1_BYTES)

    # UTF-8 output identical to the previous text-mode read.
    utf8 = service.get_file_at_revision(path=UTF8_FILE, revision="HEAD")
    assert utf8.content == _git(repo, "show", f"HEAD:{UTF8_FILE}")
    assert utf8.size_bytes == len(UTF8_BYTES)


def _mcp_body(response: Dict[str, Any]) -> Dict[str, Any]:
    body: Dict[str, Any] = json.loads(response["content"][0]["text"])
    return body


def test_mcp_handler_returns_latin1_text(repo: Path) -> None:
    from code_indexer.server.mcp.handlers import git_read

    legacy = SimpleNamespace(
        _resolve_git_repo_path=lambda alias, username: (str(repo), None)
    )
    with patch.object(git_read, "_get_legacy", return_value=legacy):
        response = git_read.handle_git_file_at_revision(
            {
                "repository_alias": "example-repo-global",
                "path": LATIN1_FILE,
                "revision": "HEAD",
            },
            USER,
        )

    body = _mcp_body(response)
    assert body["success"] is True, body
    assert body["content"] == "/// Caf\xe9 image generator\nclass G { }\n"
    assert body["size_bytes"] == len(LATIN1_BYTES)
