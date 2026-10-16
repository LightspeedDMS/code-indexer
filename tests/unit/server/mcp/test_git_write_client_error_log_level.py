"""MCP git write tools log a rejected caller argument as a client error.

Invariant: a git argument the caller supplied and the server rejected
(``GitArgumentValidationError``) logs at WARNING without a traceback and
is never reported as an unexpected error. The response still names the
rejected argument.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional, Tuple
from unittest.mock import patch

import pytest

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.mcp.handlers import git_write
from tests.unit.server.services._git_confirm_helpers import make_repo

_LOGGER = "code_indexer.server.mcp.handlers"


def _admin() -> User:
    return User(
        username="example-user",
        role=UserRole.ADMIN,
        password_hash="unused",
        created_at=datetime.now(),
    )


def test_git_push_invalid_remote_logs_warning_without_traceback(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    repo = make_repo(tmp_path, "example-repo")

    def _resolve(alias: str, username: str) -> Tuple[Optional[str], Optional[str]]:
        return str(repo), None

    caplog.set_level(logging.DEBUG, logger=_LOGGER)
    with patch(
        "code_indexer.server.mcp.handlers._legacy._resolve_git_repo_path",
        side_effect=_resolve,
    ):
        response = git_write.git_push(
            {"repository_alias": "example-repo", "remote": "-oops"}, _admin()
        )
    body: Dict[str, Any] = json.loads(response["content"][0]["text"])

    assert body["success"] is False
    assert "-oops" in body["error"]
    records = [r for r in caplog.records if "git_push" in r.getMessage()]
    assert records, caplog.text
    assert all(r.levelno == logging.WARNING for r in records), records
    assert all(not r.exc_info for r in records), records
    assert not any("Unexpected error" in r.getMessage() for r in records)
