"""MCP handlers for shared technical memory CRUD (Story #877).

Thin translation layer: MCP params -> MemoryStoreService -> MCP response.
All business logic lives in MemoryStoreService (and its 174 unit tests).
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from code_indexer.server.auth.user_manager import User
from code_indexer.server.services.memory_schema import MemorySchemaValidationError
from code_indexer.server.services.memory_store_service import (
    ConflictError,
    NotFoundError,
    RateLimitError,
    StaleContentError,
)
from . import _utils
from ._utils import _mcp_response

logger = logging.getLogger(__name__)

# Parameters stripped from MCP params before forwarding to the service layer.
_EDIT_CONTROL_PARAMS = frozenset(["memory_id", "expected_content_hash"])


def _get_service():
    """Retrieve MemoryStoreService from app.state; return None if any level is absent."""
    app = getattr(_utils.app_module, "app", None)
    if app is None:
        return None
    state = getattr(app, "state", None)
    if state is None:
        return None
    return getattr(state, "memory_store_service", None)


def _service_unavailable() -> Dict[str, Any]:
    return _mcp_response(
        {"success": False, "error": "service_unavailable",
         "message": "Memory store service unavailable"}
    )


def _missing_param(name: str) -> Dict[str, Any]:
    return _mcp_response(
        {"success": False, "error": "missing_parameter",
         "message": f"Missing required parameter: {name}"}
    )


def _invalid_input(message: str) -> Dict[str, Any]:
    return _mcp_response(
        {"success": False, "error": "invalid_input", "message": message}
    )


def _validate_entry_point(params: Any, user: Any) -> Optional[Dict[str, Any]]:
    """Validate handler entry-point preconditions.

    Returns an error MCP response if preconditions fail, else None.
    """
    if not isinstance(params, dict):
        return _invalid_input("params must be a dict")
    if user is None:
        return _invalid_input("user must not be None")
    username = getattr(user, "username", None)
    if not username:
        return _invalid_input("user.username must be non-empty")
    return None


def _handle_common_exception(exc: Exception, handler_name: str) -> Dict[str, Any]:
    """Map shared service exceptions to structured MCP error responses.

    Covers: MemorySchemaValidationError, RateLimitError, ConflictError,
    NotFoundError, ValueError, and unexpected exceptions.

    StaleContentError is NOT handled here because it carries the
    handler-specific `current_hash` field that must be included inline.
    """
    if isinstance(exc, MemorySchemaValidationError):
        return _mcp_response(
            {"success": False, "error": "validation_error", "message": str(exc)}
        )
    if isinstance(exc, RateLimitError):
        return _mcp_response(
            {"success": False, "error": "rate_limit_exceeded", "message": str(exc)}
        )
    if isinstance(exc, ConflictError):
        return _mcp_response(
            {"success": False, "error": "conflict", "message": str(exc)}
        )
    if isinstance(exc, NotFoundError):
        return _mcp_response(
            {"success": False, "error": "not_found", "message": str(exc)}
        )
    if isinstance(exc, ValueError):
        return _mcp_response(
            {"success": False, "error": "invalid_input", "message": str(exc)}
        )
    logger.exception("Unexpected error in %s", handler_name)
    return _mcp_response(
        {"success": False, "error": "internal_error", "message": str(exc)}
    )


def handle_create_memory(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Create a new memory entry.

    Required params: type, scope, summary, evidence.
    Optional params: scope_target, referenced_repo, body.
    """
    entry_error = _validate_entry_point(params, user)
    if entry_error is not None:
        return entry_error

    service = _get_service()
    if service is None:
        return _service_unavailable()

    try:
        result = service.create_memory(params, user.username)
        return _mcp_response(
            {"success": True, "id": result["id"],
             "content_hash": result["content_hash"], "path": result["path"]}
        )
    except StaleContentError as e:
        return _mcp_response(
            {"success": False, "error": "stale_content_hash",
             "current_content_hash": e.current_hash, "message": str(e)}
        )
    except Exception as e:
        return _handle_common_exception(e, "handle_create_memory")


def handle_edit_memory(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Edit an existing memory entry (full-replacement PUT semantics).

    Required params: memory_id, expected_content_hash, type, scope, summary, evidence.
    Optional params: scope_target, referenced_repo, body.
    """
    entry_error = _validate_entry_point(params, user)
    if entry_error is not None:
        return entry_error

    service = _get_service()
    if service is None:
        return _service_unavailable()

    memory_id = params.get("memory_id")
    if not memory_id:
        return _missing_param("memory_id")

    expected_content_hash = params.get("expected_content_hash")
    if not expected_content_hash:
        return _missing_param("expected_content_hash")

    # Strip control params before forwarding payload to the service.
    payload = {k: v for k, v in params.items() if k not in _EDIT_CONTROL_PARAMS}

    try:
        result = service.edit_memory(memory_id, payload, expected_content_hash,
                                     user.username)
        return _mcp_response(
            {"success": True, "id": result["id"],
             "content_hash": result["content_hash"], "path": result["path"]}
        )
    except StaleContentError as e:
        return _mcp_response(
            {"success": False, "error": "stale_content_hash",
             "current_content_hash": e.current_hash, "message": str(e)}
        )
    except Exception as e:
        return _handle_common_exception(e, "handle_edit_memory")


def handle_delete_memory(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """Delete a memory entry by id with optimistic-lock check.

    Required params: memory_id, expected_content_hash.
    """
    entry_error = _validate_entry_point(params, user)
    if entry_error is not None:
        return entry_error

    service = _get_service()
    if service is None:
        return _service_unavailable()

    memory_id = params.get("memory_id")
    if not memory_id:
        return _missing_param("memory_id")

    expected_content_hash = params.get("expected_content_hash")
    if not expected_content_hash:
        return _missing_param("expected_content_hash")

    try:
        service.delete_memory(memory_id, expected_content_hash, user.username)
        return _mcp_response(
            {"success": True, "id": memory_id, "message": "Memory deleted"}
        )
    except StaleContentError as e:
        return _mcp_response(
            {"success": False, "error": "stale_content_hash",
             "current_content_hash": e.current_hash, "message": str(e)}
        )
    except Exception as e:
        return _handle_common_exception(e, "handle_delete_memory")


def _register(registry: dict) -> None:
    """Register memory handlers in HANDLER_REGISTRY."""
    registry["create_memory"] = handle_create_memory
    registry["edit_memory"] = handle_edit_memory
    registry["delete_memory"] = handle_delete_memory
