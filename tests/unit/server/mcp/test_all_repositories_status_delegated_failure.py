"""get_all_repositories_status never drops a repo because the content seam failed.

Under a delegated content backend ``get_repository_details`` raises
RepositoryStatisticsUnavailableError when the manifest cannot be read. Skipping
that repo (as other per-repo errors are skipped) would make the summary claim
the user has fewer repositories; the tool reports the failure instead.
"""

from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Dict
from unittest.mock import MagicMock

import pytest

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.mcp.handlers import _utils
from code_indexer.server.mcp.handlers import repos
from code_indexer.server.repositories.repository_listing_manager import (
    RepositoryStatisticsUnavailableError,
)


def _user() -> User:
    return User(
        username="alice",
        password_hash="x",
        role=UserRole.NORMAL_USER,
        email="alice@example.com",
        created_at=datetime.now(),
    )


def _payload(response: Dict[str, Any]) -> Dict[str, Any]:
    payload: Dict[str, Any] = json.loads(response["content"][0]["text"])
    return payload


@pytest.fixture
def listing(monkeypatch: pytest.MonkeyPatch) -> MagicMock:
    app = MagicMock()
    app.activated_repo_manager.list_activated_repositories.return_value = [
        {"user_alias": "a"},
        {"user_alias": "b"},
    ]
    monkeypatch.setattr(_utils, "app_module", app)
    monkeypatch.setattr(repos, "check_query_admission", lambda: MagicMock(allowed=True))
    monkeypatch.setattr(repos, "_append_global_repos_to_status", lambda *a: None)
    details: MagicMock = app.repository_listing_manager.get_repository_details
    return details


def test_seam_failure_is_an_error_not_a_shorter_summary(listing: MagicMock) -> None:
    listing.side_effect = [
        {"alias": "a"},
        RepositoryStatisticsUnavailableError("manifest of b failed"),
    ]

    result = _payload(repos.get_all_repositories_status({}, _user()))

    assert result["success"] is False
    assert "manifest of b failed" in result["error"]


def test_other_per_repo_errors_are_still_skipped(listing: MagicMock) -> None:
    """Parity: a stale activation (any other error) is skipped as before."""
    listing.side_effect = [{"alias": "a"}, FileNotFoundError("gone")]

    result = _payload(repos.get_all_repositories_status({}, _user()))

    assert result["success"] is True
    assert result["repositories"] == [{"alias": "a"}]
    assert result["total"] == 1
