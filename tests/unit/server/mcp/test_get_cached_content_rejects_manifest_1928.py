"""Bug #1928 round 3 (P3 -- Opus): get_cached_content (mcp search.py)
must reject a cache_handle carrying the pages-v1 discriminator prefix
with a clear, structured error pointing to cidx_fetch_cached_payload --
NOT return the raw manifest JSON as if it were ordinary paginated
content (get_cached_content's page-indexing convention is 0-indexed,
different from cidx_fetch_cached_payload's 1-indexed contract, so
routing through fetch_cached_page transparently would silently break
that convention rather than fix anything)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from code_indexer.server.mcp.handlers import xray_truncation as xt


@pytest.fixture
def mock_user():
    from unittest.mock import Mock

    from code_indexer.server.auth.user_manager import User, UserRole

    user = Mock(spec=User)
    user.username = "testuser"
    user.role = UserRole.NORMAL_USER
    user.has_permission = Mock(return_value=True)
    return user


class TestGetCachedContentRejectsManifestHandle:
    def test_pages_v1_handle_returns_structured_rejection(self, mock_user) -> None:
        from code_indexer.server.mcp.handlers.search import handle_get_cached_content

        manifest_handle = f"{xt._PAGES_V1_HANDLE_PREFIX}some-handle"
        mock_cache = MagicMock()
        mock_app = MagicMock()
        mock_app.state.payload_cache = mock_cache

        with patch(
            "code_indexer.server.mcp.handlers.search._utils.app_module",
            **{"app": mock_app},
        ):
            result = handle_get_cached_content({"handle": manifest_handle}, mock_user)

        import json

        data = json.loads(result["content"][0]["text"])
        assert data.get("success") is False
        assert "cidx_fetch_cached_payload" in data.get("message", "")
        mock_cache.retrieve.assert_not_called()
