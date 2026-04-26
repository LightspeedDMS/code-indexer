"""Unit tests for MCP memory handlers (Story #877).

Tests cover handle_create_memory, handle_edit_memory, and handle_delete_memory.
The service layer (MemoryStoreService) is mocked because handlers are thin
translators — the service has its own 174 unit tests.
"""

import json
from typing import Any, cast
from unittest.mock import MagicMock, patch

from code_indexer.server.services.memory_store_service import (
    ConflictError,
    NotFoundError,
    RateLimitError,
    StaleContentError,
)
from code_indexer.server.services.memory_schema import MemorySchemaValidationError
from code_indexer.server.auth.user_manager import User


def make_user(username: str = "alice") -> MagicMock:
    """Return a mock User with .username set."""
    user = MagicMock(spec=User)
    user.username = username
    return user


def make_context(service_mock: MagicMock):
    """Return a context manager that patches _utils.app_module with service_mock."""
    app_module_mock = MagicMock()
    app_module_mock.app.state.memory_store_service = service_mock
    return patch(
        "code_indexer.server.mcp.handlers.memory._utils.app_module",
        app_module_mock,
    )


def _parse_response(response: dict) -> dict[Any, Any]:
    """Parse the MCP content envelope and return the inner data dict."""
    # cast needed: json.loads() returns Any; MCP handlers always return dict envelope
    return cast(dict[Any, Any], json.loads(response["content"][0]["text"]))


# ---------------------------------------------------------------------------
# handle_create_memory tests
# ---------------------------------------------------------------------------


class TestHandleCreateMemory:
    """Tests for handle_create_memory."""

    def test_success_returns_id_content_hash_path(self):
        """Test 1: success path returns success=True with id, content_hash, path."""
        from code_indexer.server.mcp.handlers.memory import handle_create_memory

        service = MagicMock()
        service.create_memory.return_value = {
            "id": "abc123",
            "content_hash": "deadbeef",
            "path": "/memories/abc123.md",
        }

        params = {
            "type": "gotcha",
            "scope": "global",
            "summary": "Some finding",
            "evidence": [{"file": "foo.py", "lines": "1-10", "quote": "x"}],
        }
        with make_context(service):
            result = handle_create_memory(params, make_user())

        data = _parse_response(result)
        assert data["success"] is True
        assert data["id"] == "abc123"
        assert data["content_hash"] == "deadbeef"
        assert data["path"] == "/memories/abc123.md"

    def test_passes_payload_to_service_create_memory(self):
        """Test 2: handler passes the raw params dict (as payload) to service.create_memory."""
        from code_indexer.server.mcp.handlers.memory import handle_create_memory

        service = MagicMock()
        service.create_memory.return_value = {
            "id": "x1",
            "content_hash": "hash1",
            "path": "/mem/x1.md",
        }

        params = {
            "type": "architectural-fact",
            "scope": "repo",
            "scope_target": "my-repo",
            "summary": "Key fact",
            "evidence": [{"file": "a.py", "lines": "5-10", "quote": "q"}],
            "body": "Extended body text",
        }
        with make_context(service):
            handle_create_memory(params, make_user("bob"))

        service.create_memory.assert_called_once_with(params, "bob")

    def test_service_unavailable_returns_error(self):
        """Test 3: when memory_store_service is None, returns service_unavailable error."""
        from code_indexer.server.mcp.handlers.memory import handle_create_memory

        app_module_mock = MagicMock()
        app_module_mock.app.state.memory_store_service = None

        params = {"type": "gotcha", "scope": "global", "summary": "s", "evidence": []}
        with patch(
            "code_indexer.server.mcp.handlers.memory._utils.app_module",
            app_module_mock,
        ):
            result = handle_create_memory(params, make_user())

        data = _parse_response(result)
        assert data["success"] is False
        assert "unavailable" in data["error"]

    def test_schema_validation_error_returns_validation_error(self):
        """Test 4: MemorySchemaValidationError -> error='validation_error'."""
        from code_indexer.server.mcp.handlers.memory import handle_create_memory

        service = MagicMock()
        service.create_memory.side_effect = MemorySchemaValidationError(
            "summary", "Summary too long"
        )

        params = {
            "type": "gotcha",
            "scope": "global",
            "summary": "x" * 2000,
            "evidence": [],
        }
        with make_context(service):
            result = handle_create_memory(params, make_user())

        data = _parse_response(result)
        assert data["success"] is False
        assert data["error"] == "validation_error"
        assert "Summary too long" in data["message"]

    def test_rate_limit_error_returns_rate_limit_exceeded(self):
        """Test 5: RateLimitError -> error='rate_limit_exceeded'."""
        from code_indexer.server.mcp.handlers.memory import handle_create_memory

        service = MagicMock()
        service.create_memory.side_effect = RateLimitError("Too many writes")

        params = {"type": "gotcha", "scope": "global", "summary": "s", "evidence": []}
        with make_context(service):
            result = handle_create_memory(params, make_user())

        data = _parse_response(result)
        assert data["success"] is False
        assert data["error"] == "rate_limit_exceeded"
        assert "Too many writes" in data["message"]

    def test_conflict_error_returns_conflict(self):
        """Test 6: ConflictError -> error='conflict'."""
        from code_indexer.server.mcp.handlers.memory import handle_create_memory

        service = MagicMock()
        service.create_memory.side_effect = ConflictError("Lock held")

        params = {"type": "gotcha", "scope": "global", "summary": "s", "evidence": []}
        with make_context(service):
            result = handle_create_memory(params, make_user())

        data = _parse_response(result)
        assert data["success"] is False
        assert data["error"] == "conflict"
        assert "Lock held" in data["message"]


# ---------------------------------------------------------------------------
# handle_edit_memory tests
# ---------------------------------------------------------------------------


class TestHandleEditMemory:
    """Tests for handle_edit_memory."""

    def test_success_returns_true_with_fields(self):
        """Test 7: edit success -> success=True with id, content_hash, path."""
        from code_indexer.server.mcp.handlers.memory import handle_edit_memory

        service = MagicMock()
        service.edit_memory.return_value = {
            "id": "mem42",
            "content_hash": "newhash",
            "path": "/mem/mem42.md",
        }

        params = {
            "memory_id": "mem42",
            "expected_content_hash": "oldhash",
            "type": "gotcha",
            "scope": "global",
            "summary": "Updated",
            "evidence": [],
        }
        with make_context(service):
            result = handle_edit_memory(params, make_user())

        data = _parse_response(result)
        assert data["success"] is True
        assert data["id"] == "mem42"
        assert data["content_hash"] == "newhash"

    def test_passes_correct_args_to_service_edit_memory(self):
        """Test 8: edit calls service.edit_memory(memory_id, payload, expected_hash, username)."""
        from code_indexer.server.mcp.handlers.memory import handle_edit_memory

        service = MagicMock()
        service.edit_memory.return_value = {
            "id": "m1",
            "content_hash": "h2",
            "path": "/mem/m1.md",
        }

        params = {
            "memory_id": "m1",
            "expected_content_hash": "h1",
            "type": "config-behavior",
            "scope": "file",
            "scope_target": "src/foo.py",
            "summary": "New summary",
            "evidence": [{"file": "src/foo.py", "lines": "1-5", "quote": "q"}],
            "body": "detail",
        }
        with make_context(service):
            handle_edit_memory(params, make_user("carol"))

        # Payload passed to edit_memory must exclude memory_id and expected_content_hash
        expected_payload = {
            "type": "config-behavior",
            "scope": "file",
            "scope_target": "src/foo.py",
            "summary": "New summary",
            "evidence": [{"file": "src/foo.py", "lines": "1-5", "quote": "q"}],
            "body": "detail",
        }
        service.edit_memory.assert_called_once_with(
            "m1", expected_payload, "h1", "carol"
        )

    def test_missing_memory_id_returns_missing_parameter(self):
        """Test 9: missing memory_id -> error='missing_parameter'."""
        from code_indexer.server.mcp.handlers.memory import handle_edit_memory

        service = MagicMock()
        params = {
            "expected_content_hash": "abc",
            "type": "gotcha",
            "scope": "global",
            "summary": "s",
            "evidence": [],
        }
        with make_context(service):
            result = handle_edit_memory(params, make_user())

        data = _parse_response(result)
        assert data["success"] is False
        assert data["error"] == "missing_parameter"
        assert "memory_id" in data["message"]

    def test_missing_expected_content_hash_returns_missing_parameter(self):
        """Test 10: missing expected_content_hash -> error='missing_parameter'."""
        from code_indexer.server.mcp.handlers.memory import handle_edit_memory

        service = MagicMock()
        params = {
            "memory_id": "x1",
            "type": "gotcha",
            "scope": "global",
            "summary": "s",
            "evidence": [],
        }
        with make_context(service):
            result = handle_edit_memory(params, make_user())

        data = _parse_response(result)
        assert data["success"] is False
        assert data["error"] == "missing_parameter"
        assert "expected_content_hash" in data["message"]

    def test_stale_content_error_returns_stale_content_hash(self):
        """Test 11: StaleContentError -> error='stale_content_hash' + current_content_hash."""
        from code_indexer.server.mcp.handlers.memory import handle_edit_memory

        service = MagicMock()
        service.edit_memory.side_effect = StaleContentError(
            "currenthash99", "Hash mismatch"
        )

        params = {
            "memory_id": "m1",
            "expected_content_hash": "wronghash",
            "type": "gotcha",
            "scope": "global",
            "summary": "s",
            "evidence": [],
        }
        with make_context(service):
            result = handle_edit_memory(params, make_user())

        data = _parse_response(result)
        assert data["success"] is False
        assert data["error"] == "stale_content_hash"
        assert data["current_content_hash"] == "currenthash99"
        assert "Hash mismatch" in data["message"]

    def test_not_found_error_returns_not_found(self):
        """Test 12: NotFoundError -> error='not_found'."""
        from code_indexer.server.mcp.handlers.memory import handle_edit_memory

        service = MagicMock()
        service.edit_memory.side_effect = NotFoundError("Memory m99 not found")

        params = {
            "memory_id": "m99",
            "expected_content_hash": "h",
            "type": "gotcha",
            "scope": "global",
            "summary": "s",
            "evidence": [],
        }
        with make_context(service):
            result = handle_edit_memory(params, make_user())

        data = _parse_response(result)
        assert data["success"] is False
        assert data["error"] == "not_found"
        assert "m99" in data["message"]


# ---------------------------------------------------------------------------
# handle_delete_memory tests
# ---------------------------------------------------------------------------


class TestHandleDeleteMemory:
    """Tests for handle_delete_memory."""

    def test_success_returns_true_with_id_and_message(self):
        """Test 13: delete success -> success=True, id, and message."""
        from code_indexer.server.mcp.handlers.memory import handle_delete_memory

        service = MagicMock()
        service.delete_memory.return_value = None

        params = {"memory_id": "del1", "expected_content_hash": "h123"}
        with make_context(service):
            result = handle_delete_memory(params, make_user())

        data = _parse_response(result)
        assert data["success"] is True
        assert data["id"] == "del1"
        assert "deleted" in data["message"].lower()

    def test_missing_expected_content_hash_returns_missing_parameter(self):
        """Test 14: delete missing expected_content_hash -> error='missing_parameter'."""
        from code_indexer.server.mcp.handlers.memory import handle_delete_memory

        service = MagicMock()
        params = {"memory_id": "del2"}
        with make_context(service):
            result = handle_delete_memory(params, make_user())

        data = _parse_response(result)
        assert data["success"] is False
        assert data["error"] == "missing_parameter"
        assert "expected_content_hash" in data["message"]

    def test_stale_content_error_returns_stale_content_hash(self):
        """Test 15: delete StaleContentError -> error='stale_content_hash' + current_content_hash."""
        from code_indexer.server.mcp.handlers.memory import handle_delete_memory

        service = MagicMock()
        service.delete_memory.side_effect = StaleContentError(
            "actualhash", "Hash mismatch on delete"
        )

        params = {"memory_id": "del3", "expected_content_hash": "wronghash"}
        with make_context(service):
            result = handle_delete_memory(params, make_user())

        data = _parse_response(result)
        assert data["success"] is False
        assert data["error"] == "stale_content_hash"
        assert data["current_content_hash"] == "actualhash"
        assert "Hash mismatch on delete" in data["message"]
