"""
Tests for Story #981: Branch-Aware Exploration for Normal Users.

AC4: Normal user calling switch_branch on a *-global alias returns 403.
AC3: Normal user calling switch_branch on their own workspace proceeds (not 403).
"""

from datetime import datetime, timezone

import pytest
from fastapi.testclient import TestClient

from code_indexer.server.app import app
from code_indexer.server.auth.dependencies import get_current_user_hybrid
from code_indexer.server.auth.user_manager import User, UserRole


def _make_normal_user() -> User:
    return User(
        username="normaluser",
        password_hash="hashed",
        role=UserRole.NORMAL_USER,
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )


@pytest.fixture
def normal_user_client():
    """TestClient with normal user bypassing JWT."""
    user = _make_normal_user()
    app.dependency_overrides[get_current_user_hybrid] = lambda: user
    yield TestClient(app, raise_server_exceptions=False)
    app.dependency_overrides.clear()


class TestSwitchBranchGlobalAliasRejection:
    """AC4: switch_branch on *-global aliases must return 403."""

    def test_switch_branch_global_alias_returns_403(self, normal_user_client):
        """Canonical -global suffix is rejected."""
        response = normal_user_client.post(
            "/api/activated-repos/evolution-global/branch",
            json={"branch_name": "main"},
        )
        assert response.status_code == 403

    def test_switch_branch_any_global_suffix_returns_403(self, normal_user_client):
        """Any alias ending in -global must be rejected."""
        for alias in ["myrepo-global", "other-repo-global", "x-global"]:
            response = normal_user_client.post(
                f"/api/activated-repos/{alias}/branch",
                json={"branch_name": "feature-branch"},
            )
            assert response.status_code == 403, (
                f"Expected 403 for alias '{alias}', got {response.status_code}"
            )

    def test_switch_branch_global_alias_error_message(self, normal_user_client):
        """403 response must include descriptive detail message."""
        response = normal_user_client.post(
            "/api/activated-repos/knowledge-base-global/branch",
            json={"branch_name": "develop"},
        )
        assert response.status_code == 403
        body = response.json()
        assert "detail" in body
        assert "global" in body["detail"].lower()

    def test_switch_branch_non_global_alias_not_403(self, normal_user_client):
        """AC3: Personal workspace alias must not be blocked by the global-alias check.

        The repo path won't exist so we'll get 404 or 500 from the manager,
        but NOT a 403 (which would mean the global-alias guard fired).
        """
        response = normal_user_client.post(
            "/api/activated-repos/my-workspace/branch",
            json={"branch_name": "main"},
        )
        # 404 (not found) or 500 (manager error) are acceptable.
        # 403 means the global-alias guard wrongly fired — that's the bug.
        assert response.status_code != 403

    def test_switch_branch_partial_global_in_middle_not_403(self, normal_user_client):
        """An alias containing 'global' but not as a suffix must NOT be rejected."""
        # "global-context" does NOT end in "-global", so the check must not fire.
        response = normal_user_client.post(
            "/api/activated-repos/global-context/branch",
            json={"branch_name": "main"},
        )
        # Anything except 403 is acceptable here (likely 404/500 from missing repo).
        assert response.status_code != 403
