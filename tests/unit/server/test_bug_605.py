"""Tests for Bug #605 fixes:

Bug A: _resolve_golden_repo_path missing -global suffix fallback
Bug B: except Exception swallows HTTPException as 500
"""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from code_indexer.server.auth.dependencies import (
    get_current_admin_user,
    get_current_admin_user_hybrid,
    get_current_user,
)
from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.mcp.handlers import _resolve_golden_repo_path


def _make_admin() -> User:
    return User(
        username="testadmin",
        password_hash="hashed",
        role=UserRole.ADMIN,
        created_at=datetime(2025, 1, 1, tzinfo=timezone.utc),
    )


def _click_global_read_alias(alias: str):
    """Returns '/repos/click' only for 'click-global'; None otherwise."""
    if alias == "click-global":
        return "/repos/click"
    return None


class TestResolveGoldenRepoPathGlobalSuffixFallback:
    """Bug A: _resolve_golden_repo_path must try alias + '-global' when base alias not found."""

    def test_resolve_golden_repo_path_uses_global_suffix_fallback(self):
        """Test A: When base alias 'click' not found, try 'click-global' and return its path."""
        with (
            patch(
                "code_indexer.server.mcp.handlers._get_golden_repos_dir",
                return_value="/fake/golden-repos",
            ),
            patch("code_indexer.server.mcp.handlers.AliasManager") as MockAliasManager,
        ):
            mock_instance = MagicMock()
            mock_instance.read_alias.side_effect = _click_global_read_alias
            MockAliasManager.return_value = mock_instance

            result = _resolve_golden_repo_path("click")

        assert result == "/repos/click", (
            "_resolve_golden_repo_path must fall back to '<alias>-global' "
            "when base alias returns None"
        )

    def test_resolve_golden_repo_path_works_with_global_alias_directly(self):
        """Test B: When caller passes 'click-global', resolves correctly without double-suffix."""
        with (
            patch(
                "code_indexer.server.mcp.handlers._get_golden_repos_dir",
                return_value="/fake/golden-repos",
            ),
            patch("code_indexer.server.mcp.handlers.AliasManager") as MockAliasManager,
        ):
            mock_instance = MagicMock()
            mock_instance.read_alias.side_effect = _click_global_read_alias
            MockAliasManager.return_value = mock_instance

            result = _resolve_golden_repo_path("click-global")

        assert result == "/repos/click", (
            "_resolve_golden_repo_path must work when caller already passes the '-global' alias"
        )

        # Verify no double-suffix: 'click-global-global' must never be attempted
        with (
            patch(
                "code_indexer.server.mcp.handlers._get_golden_repos_dir",
                return_value="/fake/golden-repos",
            ),
            patch("code_indexer.server.mcp.handlers.AliasManager") as MockAliasManager,
        ):
            mock_instance = MagicMock()
            mock_instance.read_alias.side_effect = _click_global_read_alias
            MockAliasManager.return_value = mock_instance

            _resolve_golden_repo_path("click-global")

            called_aliases = [
                call.args[0] for call in mock_instance.read_alias.call_args_list
            ]
            assert "click-global-global" not in called_aliases, (
                "_resolve_golden_repo_path must NOT try double '-global' suffix"
            )

    def test_resolve_returns_none_when_neither_alias_found(self):
        """Test A edge case: Returns None when both base and -global alias are missing."""
        with (
            patch(
                "code_indexer.server.mcp.handlers._get_golden_repos_dir",
                return_value="/fake/golden-repos",
            ),
            patch("code_indexer.server.mcp.handlers.AliasManager") as MockAliasManager,
        ):
            mock_instance = MagicMock()
            mock_instance.read_alias.return_value = None
            MockAliasManager.return_value = mock_instance

            result = _resolve_golden_repo_path("nonexistent")

        assert result is None, (
            "_resolve_golden_repo_path must return None when alias not found under any form"
        )


class TestAddGoldenRepoIndexReturns404NotFound:
    """Bug B: HTTPException(404) from _resolve_golden_repo_path must not be swallowed as 500."""

    @pytest.fixture
    def admin_client(self):
        """TestClient with admin bypassing JWT, using the full app."""
        from code_indexer.server.app import app

        admin = _make_admin()
        app.dependency_overrides[get_current_user] = lambda: admin
        app.dependency_overrides[get_current_admin_user] = lambda: admin
        app.dependency_overrides[get_current_admin_user_hybrid] = lambda: admin
        yield TestClient(app, raise_server_exceptions=False)
        app.dependency_overrides.clear()

    def test_add_golden_repo_index_returns_404_not_500_when_alias_missing(
        self, admin_client
    ):
        """Test C: POST .../indexes returns 404 (not 500) when alias doesn't exist."""
        # _resolve_golden_repo_path is imported locally inside the function body,
        # so patch at the source module (handlers), not at inline_admin_ops.
        with patch(
            "code_indexer.server.mcp.handlers._resolve_golden_repo_path",
            return_value=None,
        ):
            response = admin_client.post(
                "/api/admin/golden-repos/nonexistent-alias/indexes",
                json={
                    "index_type": "semantic",
                    "providers": ["voyage-ai"],
                },
            )

        assert response.status_code == 404, (
            f"Expected 404 when alias not found, got {response.status_code}. "
            f"Response: {response.text}"
        )
        assert response.status_code != 500, (
            "HTTPException(404) must NOT be swallowed and re-raised as 500"
        )
