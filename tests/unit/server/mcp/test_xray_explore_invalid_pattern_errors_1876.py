"""Bug #1876 N5: xray_explore's front door must validate
include_patterns/exclude_patterns exactly like its sibling xray_search.

`handle_xray_explore` (src/code_indexer/server/mcp/handlers/xray/_explore.py) never
called `_validate_xray_search_patterns` at all -- unlike `handle_xray_search`,
which validates via that helper before job submission. Two concrete
consequences: (1) a non-string item or malformed glob passed straight into
the background job with no structured error, and (2) a BARE STRING passed as
include_patterns/exclude_patterns (e.g. "*.md" instead of ["*.md"]) is
silently ``list()``-exploded into one single-character pattern per
character ("*", ".", "m", "d") instead of being rejected -- the exact
"iterated character by character" defect called out in the issue.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict, cast
from unittest.mock import patch

from code_indexer.server.auth.user_manager import User, UserRole


def _make_user(role: UserRole = UserRole.NORMAL_USER) -> User:
    return User(
        username="testuser",
        password_hash="$2b$12$x",
        role=role,
        created_at=datetime(2024, 1, 1, tzinfo=timezone.utc),
    )


def _parse_response(result: Dict[str, Any]) -> Dict[str, Any]:
    return cast(Dict[str, Any], json.loads(result["content"][0]["text"]))


VALID_PARAMS: Dict[str, Any] = {
    "repository_alias": "myrepo-global",
    "pattern": r"prepareStatement",
    "evaluator_code": "fn evaluate_node(node: &OwnedNode) -> Vec<EvalFinding> { vec![] }",
    "search_target": "content",
}


def _import_handler():
    from code_indexer.server.mcp.handlers.xray import handle_xray_explore

    return handle_xray_explore


async def test_invalid_include_patterns_rejected_by_real_handler():
    """A non-string include_patterns item is rejected by the REAL
    handle_xray_explore dispatch path (not merely the standalone
    _validate_xray_search_patterns helper in isolation)."""
    user = _make_user(UserRole.NORMAL_USER)
    params = {**VALID_PARAMS, "include_patterns": [None]}

    with patch(
        "code_indexer.server.mcp.handlers.xray._explore._resolve_repo_path",
        return_value="/some/path",
    ):
        result = await _import_handler()(params, user)

    data = _parse_response(result)
    assert data.get("error") == "include_patterns_invalid"


async def test_invalid_exclude_patterns_rejected_by_real_handler():
    """A malformed exclude_patterns glob (unbalanced brace) is rejected by
    the REAL handle_xray_explore dispatch path."""
    user = _make_user(UserRole.NORMAL_USER)
    params = {**VALID_PARAMS, "exclude_patterns": ["*.{ts,md"]}

    with patch(
        "code_indexer.server.mcp.handlers.xray._explore._resolve_repo_path",
        return_value="/some/path",
    ):
        result = await _import_handler()(params, user)

    data = _parse_response(result)
    assert data.get("error") == "exclude_patterns_invalid"


async def test_bare_string_include_patterns_rejected_not_iterated_per_character():
    """The specific N5 defect: a bare string ("*.md") must be REJECTED as
    include_patterns_invalid, never silently accepted and exploded into
    one pattern per character via list("*.md") == ['*', '.', 'm', 'd']."""
    user = _make_user(UserRole.NORMAL_USER)
    params = {**VALID_PARAMS, "include_patterns": "*.md"}

    with patch(
        "code_indexer.server.mcp.handlers.xray._explore._resolve_repo_path",
        return_value="/some/path",
    ):
        result = await _import_handler()(params, user)

    data = _parse_response(result)
    assert data.get("error") == "include_patterns_invalid"


async def test_bare_string_exclude_patterns_rejected_not_iterated_per_character():
    user = _make_user(UserRole.NORMAL_USER)
    params = {**VALID_PARAMS, "exclude_patterns": "*.md"}

    with patch(
        "code_indexer.server.mcp.handlers.xray._explore._resolve_repo_path",
        return_value="/some/path",
    ):
        result = await _import_handler()(params, user)

    data = _parse_response(result)
    assert data.get("error") == "exclude_patterns_invalid"
