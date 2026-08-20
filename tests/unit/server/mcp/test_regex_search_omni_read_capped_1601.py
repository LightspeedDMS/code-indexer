"""Unit tests for Issue #1601 AC-E/G1 -- the MCP omni regex_search response
must OR the per-repo read_capped signal, mirroring how truncated is already
OR'd across repos, rather than silently dropping it.
"""

import json
from typing import cast
from unittest.mock import Mock, patch

import pytest

from code_indexer.server.mcp.handlers.search import _omni_regex_search
from code_indexer.server.auth.user_manager import User, UserRole


@pytest.fixture
def mock_user():
    user = Mock(spec=User)
    user.username = "testuser"
    user.role = UserRole.NORMAL_USER
    user.has_permission = Mock(return_value=True)
    return user


def _mcp_response(read_capped: bool) -> dict:
    payload = {
        "success": True,
        "matches": [],
        "truncated": False,
        "read_capped": read_capped,
    }
    return {"content": [{"type": "text", "text": json.dumps(payload)}]}


async def _run_omni(args: dict, user, per_repo_read_capped: dict) -> dict:
    """Invoke _omni_regex_search with wildcard expansion/cap-check bypassed
    and the recursive handle_regex_search call replaced by a canned
    response per repository alias, keyed by ``per_repo_read_capped``."""

    async def _fake_handle_regex_search(single_args, _user):
        return _mcp_response(per_repo_read_capped[single_args["repository_alias"]])

    with (
        patch(
            "code_indexer.server.mcp.handlers.search._expand_wildcard_patterns",
            return_value=list(per_repo_read_capped.keys()),
        ),
        patch(
            "code_indexer.server.mcp.handlers.search._enforce_repo_count_cap",
            return_value=None,
        ),
        patch(
            "code_indexer.server.mcp.handlers.search.handle_regex_search",
            side_effect=_fake_handle_regex_search,
        ),
    ):
        result = await _omni_regex_search(args, user)

    return cast(dict, json.loads(result["content"][0]["text"]))


class TestOmniRegexSearchReadCappedSurfacing:
    """AC-E/G1: read_capped is OR'd across constituent repos, like truncated."""

    @pytest.mark.asyncio
    async def test_read_capped_true_when_any_repo_was_capped(self, mock_user):
        args = {"repository_alias": ["repo1-global", "repo2-global"], "pattern": "func"}
        data = await _run_omni(
            args, mock_user, {"repo1-global": False, "repo2-global": True}
        )
        assert data["read_capped"] is True

    @pytest.mark.asyncio
    async def test_read_capped_false_when_no_repo_was_capped(self, mock_user):
        args = {"repository_alias": ["repo1-global", "repo2-global"], "pattern": "func"}
        data = await _run_omni(
            args, mock_user, {"repo1-global": False, "repo2-global": False}
        )
        assert data["read_capped"] is False
