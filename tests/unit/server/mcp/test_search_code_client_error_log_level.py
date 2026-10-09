"""MCP search_code logs a failure at the level its classification implies.

Invariant (shared with REST /api/query via
``search_error_policy.classify_search_error``): a client error -- a request
the caller can fix -- logs at WARNING without a traceback; every other
failure logs at ERROR with ``exc_info``. The response body is unchanged.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any, Dict, List

import pytest

from code_indexer.server.auth.user_manager import User, UserRole
from code_indexer.server.mcp.handlers.search.code_search import search_code

_LOGGER = "code_indexer.server.mcp.handlers.search"


def _admin() -> User:
    return User(
        username="example-user",
        role=UserRole.ADMIN,
        password_hash="unused",
        created_at=datetime.now(),
    )


def _search(params: Dict[str, Any]) -> Dict[str, Any]:
    response = search_code({"query_text": "x", **params}, _admin())
    return json.loads(response["content"][0]["text"])  # type: ignore[no-any-return]


def _search_failures(caplog: pytest.LogCaptureFixture) -> List[logging.LogRecord]:
    return [r for r in caplog.records if "Error in search_code" in r.getMessage()]


@pytest.mark.parametrize("time_range", ["bogus", "2026-01-01"])
def test_client_error_logs_warning_without_traceback(
    caplog: pytest.LogCaptureFixture, time_range: str
) -> None:
    caplog.set_level(logging.DEBUG, logger=_LOGGER)

    body = _search({"repository_alias": "example-repo", "time_range": time_range})

    assert body["success"] is False
    assert "Time range" in body["error"]
    records = _search_failures(caplog)
    assert records, caplog.text
    assert all(r.levelno == logging.WARNING for r in records), records
    assert all(not r.exc_info for r in records), records


def test_internal_failure_logs_error_with_traceback(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.DEBUG, logger=_LOGGER)

    # A global alias with no server state wired fails inside the server.
    body = _search({"repository_alias": "example-repo-global"})

    assert body["success"] is False
    records = _search_failures(caplog)
    assert any(r.levelno == logging.ERROR and r.exc_info for r in records), records
