"""Regression tests for Bug #1802.

`GroupAccessManager.log_audit(details=...)` used to accept a bare free-text
string. The reader, `_decode_audit_log_details`
(mcp/handlers/admin/__init__.py), always `json.loads()`s the stored
`details` column -- so every free-text writer produced a row the reader
could not parse, and `handle_query_audit_logs` emitted a malformed-JSON
WARNING per affected row instead of returning usable structured detail.

The fix makes the contract impossible to get wrong: `log_audit()` now
requires `details` to be a dict (or None) and serializes it internally, so
there is exactly ONE JSON-serialization path for every one of its 22
call sites. Legacy free-text rows written before this fix must still be
readable without crashing and without being silently treated as empty --
they are surfaced as ``{"raw": <original text>}``.

These tests use REAL GroupAccessManager/AuditLogService instances backed
by a real temporary SQLite database (no mocks), and drive the actual MCP
front-door handlers (`handle_create_group`, `handle_query_audit_logs`) --
matching the project's anti-mock convention and the sibling test file
test_query_audit_logs_pagination_dedup_1646_1647.py.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Dict

import pytest

from code_indexer.server.services.audit_log_service import AuditLogService
from code_indexer.server.services.group_access_manager import GroupAccessManager


@pytest.fixture
def admin_user():
    from code_indexer.server.auth.user_manager import User, UserRole

    return User(
        username="admin",
        password_hash="x",
        role=UserRole.ADMIN,
        created_at=datetime.now(timezone.utc),
    )


@pytest.fixture
def audit_service(tmp_path):
    """Real AuditLogService backed by a real temp SQLite file."""
    db_path = tmp_path / "audit_1802.db"
    return AuditLogService(db_path)


@pytest.fixture
def group_manager(tmp_path, audit_service):
    """Real GroupAccessManager wired to the real AuditLogService above."""
    db_path = tmp_path / "groups_1802.db"
    manager = GroupAccessManager(db_path)
    manager.set_audit_service(audit_service)
    return manager


@pytest.fixture
def wired_app_state(audit_service, group_manager):
    """Install the real services onto app.state for the test duration."""
    import code_indexer.server.app as app_module

    sentinel = object()
    prev_audit = getattr(app_module.app.state, "audit_service", sentinel)
    prev_group = getattr(app_module.app.state, "group_manager", sentinel)
    app_module.app.state.audit_service = audit_service
    app_module.app.state.group_manager = group_manager
    try:
        yield
    finally:
        if prev_audit is sentinel:
            del app_module.app.state.audit_service
        else:
            app_module.app.state.audit_service = prev_audit
        if prev_group is sentinel:
            del app_module.app.state.group_manager
        else:
            app_module.app.state.group_manager = prev_group


def _entries_for_action(
    payload: dict, action_type: str, target_id: str
) -> Dict[str, Any]:
    matches: list[Dict[str, Any]] = [
        e
        for e in payload["entries"]
        if e.get("action_type") == action_type and e.get("target_id") == target_id
    ]
    assert len(matches) == 1, (
        f"expected exactly 1 entry for action_type={action_type!r} "
        f"target_id={target_id!r}, found {len(matches)}"
    )
    return matches[0]


class TestStructuredRoundTripThroughFrontDoor:
    """Test 1: an action that writes an audit entry, queried back through the
    MCP front door, returns the expected STRUCTURED object -- not merely
    "no warning was logged" (that would pass for json.dumps(""))."""

    def test_create_group_audit_details_decode_as_structured_object(
        self, admin_user, wired_app_state
    ):
        from code_indexer.server.mcp.handlers.admin import (
            handle_create_group,
            handle_query_audit_logs,
        )

        create_result = handle_create_group.__wrapped__(
            {"name": "structured-test", "description": "structured test group"},
            admin_user,
        )
        create_payload = json.loads(create_result["content"][0]["text"])
        assert create_payload["success"] is True
        group_id = str(create_payload["group_id"])

        query_result = handle_query_audit_logs.__wrapped__(
            {"action": "group_create"}, admin_user
        )
        query_payload = json.loads(query_result["content"][0]["text"])
        assert query_payload["success"] is True

        entry = _entries_for_action(query_payload, "group_create", group_id)

        # The whole point of the fix: details is a genuine structured
        # object with the real field values, not an empty/opaque blob.
        assert entry["details"] == {
            "name": "structured-test",
            "description": "structured test group",
            "source": "mcp",
        }


class TestLegacyFreeTextRowIsHandledWithoutCrashingOrHidingContent:
    """Test 2: a legacy free-text row already in the store is handled per
    the documented contract, without crashing and without being silently
    treated as empty."""

    def test_legacy_free_text_row_surfaces_as_raw_not_empty(
        self, admin_user, wired_app_state, audit_service
    ):
        from code_indexer.server.mcp.handlers.admin import handle_query_audit_logs

        legacy_text = "Created group 'legacy' via MCP"
        audit_service.log_raw(
            timestamp="2026-08-20T10:00:00+00:00",
            admin_id="admin",
            action_type="group_create",
            target_type="group",
            target_id="999",
            details=legacy_text,
        )

        result = handle_query_audit_logs.__wrapped__(
            {"action": "group_create"}, admin_user
        )
        payload = json.loads(result["content"][0]["text"])
        assert payload["success"] is True

        entry = _entries_for_action(payload, "group_create", "999")

        # Must not crash (getting here proves that) and must not silently
        # pretend the row is empty: the original free text is preserved.
        assert entry["details"] == {"raw": legacy_text}


class TestLogAuditRejectsBareStringDetails:
    """Test 3: since the log_audit signature changed to require a dict, a
    caller passing a bare string is now a type error, proving the contract
    is enforced rather than merely conventional."""

    def test_log_audit_raises_type_error_for_string_details(self, group_manager):
        with pytest.raises(TypeError):
            group_manager.log_audit(
                admin_id="admin",
                action_type="group_create",
                target_type="group",
                target_id="1",
                # Intentionally violates the typed contract to verify
                # runtime enforcement of the dict-only details parameter.
                details="free text is no longer accepted",  # type: ignore[arg-type]
            )
