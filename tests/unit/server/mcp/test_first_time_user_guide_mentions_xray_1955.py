"""
Bug #1955 item 6: first_time_user_guide (5143 chars) never mentioned X-Ray or
analyze_graph -- a first-time user following the onboarding guide had no path
from "I found the code I care about" to this server's core cross-file
capability (dead code, unwired components, layering violations,
endpoint-to-sink reachability, blast radius).
"""

import json

from code_indexer.server.mcp.handlers.guides import first_time_user_guide
from code_indexer.server.auth.user_manager import User, UserRole
from datetime import datetime


def _extract_mcp_data(mcp_response: dict) -> dict:
    content = mcp_response.get("content", [])
    if content and content[0].get("type") == "text":
        return json.loads(content[0]["text"])  # type: ignore[no-any-return]
    return {}


def test_first_time_user_guide_mentions_xray_and_analyze_graph() -> None:
    user = User(
        username="test",
        password_hash="hashed_password",
        role=UserRole.NORMAL_USER,
        created_at=datetime.now(),
    )

    mcp_response = first_time_user_guide({}, user)
    result = _extract_mcp_data(mcp_response)

    guide_text = json.dumps(result)
    assert "X-Ray" in guide_text, (
        "first_time_user_guide must mention X-Ray -- the onboarding guide "
        "currently has no path to this server's cross-file analysis capability"
    )
    assert "analyze_graph" in guide_text, (
        "first_time_user_guide must mention analyze_graph by name so a "
        "first-time user can discover it"
    )
