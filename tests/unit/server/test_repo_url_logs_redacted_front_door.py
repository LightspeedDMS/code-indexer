# ruff: noqa: F811
"""Server logs record repository URLs with their userinfo redacted.

Every log record, at DEBUG and above, is captured while the real app
activates a repository whose origin carries userinfo, switches its branch
through MCP (the fetch from the example remote fails), updates the stored
golden URL, and checks an unreachable URL's accessibility.

Hosts, usernames and secrets are neutral placeholders.
"""

from __future__ import annotations

import logging
from typing import Any

import pytest
from fastapi.testclient import TestClient

from tests.unit.server.repo_url_userinfo_env import (  # noqa: F401 - fixtures
    REPO,
    SECRET,
    USER,
    USERINFO_URL,
    activate_for_user,
    app,
    client,
    mcp_call,
)

LOG_ACTIVATION = "log-check-repo"


def test_activation_and_branch_switch_log_no_userinfo(
    client: TestClient,
    app: Any,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.DEBUG):
        activate_for_user(app, monkeypatch, user_alias=LOG_ACTIVATION)
        mcp_call(
            client,
            app,
            USER,
            "switch_branch",
            {"user_alias": LOG_ACTIVATION, "branch_name": "no-such-branch"},
        )
    assert "git.example.com" in caplog.text
    assert SECRET not in caplog.text


def test_golden_url_update_logs_no_userinfo(
    app: Any, caplog: pytest.LogCaptureFixture
) -> None:
    backend = app.state.golden_repo_manager._sqlite_backend
    with caplog.at_level(logging.DEBUG):
        assert backend.update_repo_url(REPO, USERINFO_URL)
    assert "git.example.com" in caplog.text
    assert SECRET not in caplog.text


def test_accessibility_check_logs_no_userinfo(
    app: Any, caplog: pytest.LogCaptureFixture
) -> None:
    manager = app.state.golden_repo_manager
    with caplog.at_level(logging.DEBUG):
        assert manager._validate_git_repository(USERINFO_URL) is False
    assert SECRET not in caplog.text
