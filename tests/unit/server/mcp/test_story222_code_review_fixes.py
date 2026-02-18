"""
Unit tests for Story #222 code review fix items.

Code review findings addressed:
  Finding 1: ToolDocLoader per-request instantiation (~650ms latency regression)
             - Cache as module-level singleton via _get_tool_doc_loader()
  Finding 2: Token budget exceeded (ADMIN 4148 vs <4000, NORMAL_USER 2263 vs <2000)
             - Remove category_filter key when null
             - Truncate tl_dr to 60 chars in handler output

TDD: These tests are written FIRST to define expected behavior.
"""

import json
from datetime import datetime
from unittest.mock import patch, MagicMock

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _extract_mcp_data(mcp_response: dict) -> dict:
    """Extract the JSON data from MCP-compliant content array response."""
    content = mcp_response.get("content", [])
    if content and content[0].get("type") == "text":
        return json.loads(content[0]["text"])
    return {}


def _count_tokens(text: str) -> int:
    """Count tokens using tiktoken cl100k_base (same as code review measurement)."""
    import tiktoken
    enc = tiktoken.get_encoding("cl100k_base")
    return len(enc.encode(text))


def _make_user(role):
    from code_indexer.server.auth.user_manager import User
    return User(
        username="test",
        password_hash="hashed",
        role=role,
        created_at=datetime.now(),
    )


def _call_quick_reference(params, user, mock_config=None):
    """Call quick_reference with standard mocks."""
    from code_indexer.server.mcp.handlers import quick_reference

    if mock_config is None:
        mock_config = MagicMock()
        mock_config.service_display_name = "Neo"
        mock_config.langfuse_config = None

    with patch("code_indexer.server.mcp.handlers.get_config_service") as mock_svc, \
         patch("code_indexer.server.mcp.handlers.app_module") as mock_app:
        mock_service = MagicMock()
        mock_service.get_config.return_value = mock_config
        mock_svc.return_value = mock_service
        mock_app.golden_repo_manager = None
        return quick_reference(params, user)


# ---------------------------------------------------------------------------
# Finding 1: ToolDocLoader singleton
# ---------------------------------------------------------------------------

class TestToolDocLoaderSingleton:
    """Finding 1: _get_tool_doc_loader() must return cached singleton."""

    def test_get_tool_doc_loader_returns_same_instance(self):
        """Calling _get_tool_doc_loader() twice must return the SAME object instance."""
        from code_indexer.server.mcp.tool_doc_loader import _get_tool_doc_loader

        first = _get_tool_doc_loader()
        second = _get_tool_doc_loader()

        assert first is second, (
            "_get_tool_doc_loader() must return the same singleton instance on "
            "repeated calls - not a new ToolDocLoader each time"
        )

    def test_get_tool_doc_loader_returns_loaded_loader(self):
        """The singleton must already have docs loaded (not require explicit load_all_docs())."""
        from code_indexer.server.mcp.tool_doc_loader import _get_tool_doc_loader, ToolDocLoader

        loader = _get_tool_doc_loader()

        assert isinstance(loader, ToolDocLoader)
        # _loaded flag indicates docs were loaded during singleton init
        assert loader._loaded is True, (
            "Singleton loader must have _loaded=True after _get_tool_doc_loader() call"
        )

    def test_get_tool_doc_loader_has_docs_in_cache(self):
        """Singleton must have tool docs populated in its cache."""
        from code_indexer.server.mcp.tool_doc_loader import _get_tool_doc_loader

        loader = _get_tool_doc_loader()
        cache = loader._cache

        assert len(cache) > 0, (
            "Singleton loader must have tool docs in cache after initialization"
        )

    def test_get_tool_doc_loader_docs_dir_is_correct(self):
        """Singleton must point to the real tool_docs directory."""
        from pathlib import Path
        from code_indexer.server.mcp.tool_doc_loader import _get_tool_doc_loader

        loader = _get_tool_doc_loader()
        expected_dir = Path(__file__).parent.parent.parent.parent.parent / "src" / "code_indexer" / "server" / "mcp" / "tool_docs"

        assert loader.docs_dir.exists(), f"tool_docs dir must exist: {loader.docs_dir}"
        assert loader.docs_dir.name == "tool_docs"


# ---------------------------------------------------------------------------
# Finding 2: Token budget
# ---------------------------------------------------------------------------

class TestTokenBudget:
    """Finding 2: Token budgets must be met for all roles."""

    def test_admin_response_under_4000_tokens(self):
        """ADMIN quick_reference response must be under 4,000 tokens."""
        from code_indexer.server.auth.user_manager import UserRole

        user = _make_user(UserRole.ADMIN)
        resp = _call_quick_reference({}, user)
        text = resp["content"][0]["text"]
        tokens = _count_tokens(text)

        assert tokens < 4000, (
            f"ADMIN response must be <4000 tokens, got {tokens} tokens "
            f"({len(text)} chars). This is a latency/context regression."
        )

    def test_normal_user_response_under_2000_tokens(self):
        """NORMAL_USER quick_reference response must be under 2,000 tokens."""
        from code_indexer.server.auth.user_manager import UserRole

        user = _make_user(UserRole.NORMAL_USER)
        resp = _call_quick_reference({}, user)
        text = resp["content"][0]["text"]
        tokens = _count_tokens(text)

        assert tokens < 2000, (
            f"NORMAL_USER response must be <2000 tokens, got {tokens} tokens "
            f"({len(text)} chars). This is a latency/context regression."
        )

    def test_category_filter_absent_when_null(self):
        """category_filter key must NOT appear in response when no filter is applied."""
        from code_indexer.server.auth.user_manager import UserRole

        user = _make_user(UserRole.ADMIN)
        resp = _call_quick_reference({}, user)  # No category param
        data = _extract_mcp_data(resp)

        assert "category_filter" not in data, (
            "category_filter must be omitted from response when null "
            "(not included as null key) - saves tokens"
        )

    def test_category_filter_present_when_provided(self):
        """category_filter key MUST appear in response when a filter IS provided."""
        from code_indexer.server.auth.user_manager import UserRole

        user = _make_user(UserRole.ADMIN)
        resp = _call_quick_reference({"category": "search"}, user)
        data = _extract_mcp_data(resp)

        assert "category_filter" in data, (
            "category_filter must be included in response when a filter is applied"
        )
        assert data["category_filter"] == "search"

    def test_tl_dr_values_truncated_to_30_chars(self):
        """All tl_dr values in the response must be <= 30 chars (token budget enforcement).

        Reduced from 42 to 30 chars to ensure standard users stay under 2000 tokens
        even when Langfuse repos with long names are configured (AC2 fix).
        """
        from code_indexer.server.auth.user_manager import UserRole

        user = _make_user(UserRole.ADMIN)
        resp = _call_quick_reference({}, user)
        data = _extract_mcp_data(resp)

        violations = []
        for category, tools in data.get("tools_by_category", {}).items():
            for tool in tools:
                tl_dr = tool["tl_dr"]
                if len(tl_dr) > 30:
                    violations.append(
                        f"{tool['name']} ({category}): {len(tl_dr)} chars - {tl_dr!r}"
                    )

        assert violations == [], (
            "All tl_dr values in response must be <=30 chars for token budget:\n"
            + "\n".join(violations)
        )

    def test_power_user_response_under_4000_tokens(self):
        """POWER_USER quick_reference response must be under 4,000 tokens."""
        from code_indexer.server.auth.user_manager import UserRole

        user = _make_user(UserRole.POWER_USER)
        resp = _call_quick_reference({}, user)
        text = resp["content"][0]["text"]
        tokens = _count_tokens(text)

        assert tokens < 4000, (
            f"POWER_USER response must be <4000 tokens, got {tokens} tokens"
        )
