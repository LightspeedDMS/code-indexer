"""MCP JSON-RPC 2.0 protocol handler.

Implements the Model Context Protocol (MCP) JSON-RPC 2.0 endpoint for tool discovery
and execution. Phase 1 implementation with stub handlers for tools/list and tools/call.
"""

from fastapi import APIRouter, Depends, Request, Response
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from typing import Dict, Any, List, Optional, Tuple, Union
from code_indexer.server.auth.dependencies import (
    get_current_user,
    get_current_user_for_mcp,
    _build_www_authenticate_header,
    _should_refresh_token,
    _refresh_jwt_cookie,
)
from code_indexer.server.auth import dependencies as auth_deps
from code_indexer.server.auth.user_manager import User
from code_indexer.server.services.config_service import get_config_service
from sse_starlette.sse import EventSourceResponse
import asyncio
import functools
import uuid
import json
import logging
from code_indexer import __version__

logger = logging.getLogger(__name__)

mcp_router = APIRouter()

# Security scheme for bearer token authentication (auto_error=False for optional auth)
security = HTTPBearer(auto_error=False)


def validate_jsonrpc_request(
    request: Dict[str, Any],
) -> Tuple[bool, Optional[Dict[str, Any]]]:
    """
    Validate JSON-RPC 2.0 request structure.

    Args:
        request: The JSON-RPC request dictionary

    Returns:
        Tuple of (is_valid, error_dict). error_dict is None if valid.
    """
    # Check jsonrpc field
    if "jsonrpc" not in request:
        return False, {
            "code": -32600,
            "message": "Invalid Request: missing 'jsonrpc' field",
        }

    if request["jsonrpc"] != "2.0":
        return False, {
            "code": -32600,
            "message": "Invalid Request: jsonrpc must be '2.0'",
        }

    # Check method field
    if "method" not in request:
        return False, {
            "code": -32600,
            "message": "Invalid Request: missing 'method' field",
        }

    if not isinstance(request["method"], str):
        return False, {
            "code": -32600,
            "message": "Invalid Request: method must be a string",
        }

    # Check params field (optional, but if present must be object or array)
    if "params" in request and request["params"] is not None:
        if not isinstance(request["params"], (dict, list)):
            return False, {
                "code": -32600,
                "message": "Invalid Request: params must be an object or array",
            }

    return True, None


def create_jsonrpc_response(
    result: Any, request_id: Union[str, int, None]
) -> Dict[str, Any]:
    """
    Create a JSON-RPC 2.0 success response.

    Args:
        result: The result data
        request_id: The request id (can be string, number, or null)

    Returns:
        JSON-RPC success response dictionary
    """
    return {"jsonrpc": "2.0", "result": result, "id": request_id}


def create_jsonrpc_error(
    code: int,
    message: str,
    request_id: Union[str, int, None],
    data: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """
    Create a JSON-RPC 2.0 error response.

    Args:
        code: Error code (e.g., -32601 for Method not found)
        message: Error message
        request_id: The request id
        data: Optional additional error data

    Returns:
        JSON-RPC error response dictionary
    """
    error_obj = {"code": code, "message": message}

    if data is not None:
        error_obj["data"] = data

    return {"jsonrpc": "2.0", "error": error_obj, "id": request_id}


def handle_tools_list(params: Dict[str, Any], user: User) -> Dict[str, Any]:
    """
    Handle tools/list method.

    Args:
        params: Request parameters
        user: Authenticated user

    Returns:
        Dictionary with tools list filtered by user role and configuration
    """
    from .tools import filter_tools_by_role

    # Story #185: Pass config to filter tools based on configuration requirements
    config = get_config_service().get_config()
    tools = filter_tools_by_role(user, config=config)
    return {"tools": tools}


async def _invoke_handler(
    handler: Any,
    arguments: Dict[str, Any],
    user: User,
    session_state: Any,
    sig: Any,
    is_async: bool,
) -> Any:
    """
    Invoke handler with appropriate parameters.

    Args:
        handler: The handler function to invoke
        arguments: Tool arguments
        user: Authenticated user
        session_state: Session state (may be None)
        sig: Handler function signature (from inspect.signature)
        is_async: Whether handler is async

    Returns:
        Handler result
    """
    if "session_state" in sig.parameters:
        if is_async:
            return await handler(arguments, user, session_state=session_state)
        else:
            loop = asyncio.get_running_loop()
            bound = functools.partial(handler, arguments, user, session_state=session_state)
            return await loop.run_in_executor(None, bound)
    else:
        if is_async:
            return await handler(arguments, user)
        else:
            loop = asyncio.get_running_loop()
            bound = functools.partial(handler, arguments, user)
            return await loop.run_in_executor(None, bound)


def _check_repository_access(
    arguments: Dict[str, Any],
    effective_user: User,
    tool_name: str,
    access_service: Any,
) -> None:
    """Check if user has access to the repository specified in tool arguments.

    Extracts the repository identifier from the arguments dict using the three
    parameter names tools use: 'repository_alias', 'alias', or 'user_alias'.
    Skips the check if no repo param is present or if the param is empty/None.

    Strips the '-global' suffix from aliases before checking, since accessible
    repos are stored without it.

    Raises ValueError if access is denied. Does nothing if user is admin or if
    no repo parameter is present.

    Args:
        arguments: Tool arguments dict from the MCP tool call
        effective_user: The effective user (may differ from authenticated user
            during impersonation)
        tool_name: Name of the tool being called (used in error messages)
        access_service: AccessFilteringService instance for access checks
    """
    # Extract the repo identifier using the three known parameter names
    raw_alias: Optional[str] = None
    # Story #331 AC3: Added "repo_alias" to protect enter_write_mode,
    # exit_write_mode, and wiki_article_analytics tools.
    for param_name in ("repository_alias", "alias", "user_alias", "repo_alias"):
        value = arguments.get(param_name)
        if value is not None and isinstance(value, str) and value:
            raw_alias = value
            break

    # No repo param present or empty - nothing to check
    if not raw_alias:
        return

    # Admin users bypass the check entirely
    if access_service.is_admin_user(effective_user.username):
        return

    # Strip -global suffix to match stored repo names
    normalized = raw_alias
    if normalized.endswith("-global"):
        normalized = normalized[: -len("-global")]

    # Check access
    accessible = access_service.get_accessible_repos(effective_user.username)
    if normalized not in accessible:
        raise ValueError(
            f"Access denied: repository '{raw_alias}' is not accessible to user"
            f" '{effective_user.username}'"
        )


async def handle_tools_call(
    params: Dict[str, Any], user: User, session_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    Handle tools/call method - dispatches to actual tool handlers.

    Args:
        params: Request parameters (must contain 'name' and optional 'arguments')
        user: Authenticated user
        session_id: Optional MCP session ID for session state management

    Returns:
        Dictionary with call result

    Raises:
        ValueError: If required parameters are missing or tool not found
    """
    from .handlers import HANDLER_REGISTRY
    from .tools import TOOL_REGISTRY
    from .session_registry import get_session_registry
    from code_indexer.server.services.langfuse_service import get_langfuse_service

    # Validate required 'name' parameter
    if "name" not in params:
        raise ValueError("Missing required parameter: name")

    tool_name = params["name"]
    arguments = params.get("arguments", {})

    # Check if tool exists
    if tool_name not in TOOL_REGISTRY:
        raise ValueError(f"Unknown tool: {tool_name}")

    # Get or create session state if session_id is provided
    session_state = None
    if session_id:
        registry = get_session_registry()
        session_state = registry.get_or_create_session(session_id, user)

    # Determine effective user for permission checks (CRITICAL 2 fix)
    # When impersonating, use the impersonated user's permissions
    effective_user = user
    if session_state and session_state.is_impersonating:
        effective_user = session_state.effective_user

    # Check if user has permission for this tool
    tool_def = TOOL_REGISTRY[tool_name]
    required_permission = tool_def["required_permission"]
    if not effective_user.has_permission(required_permission):
        raise ValueError(
            f"Permission denied: {required_permission} required for tool {tool_name}"
        )

    # Story #319: Centralized repository access guard.
    # Check repo access BEFORE invoking handler, using effective_user.
    # Lazy import to avoid circular dependency (handlers imports from protocol).
    try:
        from code_indexer.server.mcp import handlers as _handlers_module

        _access_service = _handlers_module.app_module.app.state.access_filtering_service
        _check_repository_access(
            arguments=arguments,
            effective_user=effective_user,
            tool_name=tool_name,
            access_service=_access_service,
        )
    except ValueError:
        # Re-raise access denied errors from the guard (fail-closed)
        raise
    except AttributeError:
        # Story #331 AC9: Fail-closed when access_filtering_service unavailable.
        # If the tool arguments contain a repository parameter, DENY access
        # rather than falling through (fail-open). Tools with no repo parameter
        # proceed normally.
        _has_repo_param = any(
            arguments.get(p)
            for p in ("repository_alias", "alias", "user_alias", "repo_alias")
            if (isinstance(arguments.get(p), str) and arguments.get(p))
            or (isinstance(arguments.get(p), list) and arguments.get(p))
        )
        if _has_repo_param:
            logger.warning(
                "Access filtering service not available for tool %s - DENYING access",
                tool_name,
            )
            raise ValueError(
                f"Access denied: access control service unavailable, "
                f"cannot verify access for tool '{tool_name}'"
            )
        logger.debug(
            "Access filtering service not available for tool %s (no repo param), proceeding",
            tool_name,
        )

    # Get handler function
    if tool_name not in HANDLER_REGISTRY:
        raise ValueError(f"Handler not implemented for tool: {tool_name}")

    handler = HANDLER_REGISTRY[tool_name]

    # NOTE: API metrics tracking moved to service layer (Story #4 AC2)
    # Services (file_crud_service, ssh_key_manager, git_operations_service, etc.)
    # track their own increment_other_api_call() calls to prevent double-counting.
    # search_code tracks semantic_search/other_index_search, regex_search tracks regex_search.

    # Call handler with arguments
    # Special handling for handlers that need session_state (CRITICAL 1, 3 fix)
    # Story #51: Support both sync and async handlers after conversion to sync
    from typing import cast
    import inspect

    # Check if handler accepts session_state parameter
    sig = inspect.signature(handler)
    is_async = asyncio.iscoroutinefunction(handler)

    # Determine if we should intercept this tool call with span logging
    # Exclude tracing tools to avoid recursion
    should_intercept = tool_name not in {"start_trace", "end_trace"}

    # Get langfuse service (may be None if not available/configured)
    langfuse_service = None
    try:
        langfuse_service = get_langfuse_service()
    except Exception as e:
        # Graceful degradation: if service unavailable, continue without tracing
        logger.debug("Langfuse service unavailable, skipping span logging: %s", e)

    # If we should intercept and service is available, wrap with span logging
    if should_intercept and langfuse_service:
        # Create async wrapper for handler execution
        async def handler_wrapper():
            return await _invoke_handler(
                handler, arguments, user, session_state, sig, is_async
            )

        # Execute through span interceptor
        try:
            result = await langfuse_service.span_logger.intercept_tool_call(
                session_id=session_id,
                tool_name=tool_name,
                arguments=arguments,
                handler=handler_wrapper,
                username=user.username,
            )
        except Exception as e:
            # If span logging fails, execute handler directly (graceful degradation)
            logger.debug(
                "Span logging failed for tool %s, continuing without tracing: %s",
                tool_name,
                e,
            )
            result = await handler_wrapper()
    else:
        # No interception - execute handler directly
        result = await _invoke_handler(
            handler, arguments, user, session_state, sig, is_async
        )

    return cast(Dict[str, Any], result)


async def process_jsonrpc_request(
    request: Dict[str, Any], user: User, session_id: Optional[str] = None
) -> Dict[str, Any]:
    """
    Process a single JSON-RPC 2.0 request.

    Args:
        request: The JSON-RPC request dictionary
        user: Authenticated user
        session_id: Optional MCP session ID for session state management

    Returns:
        JSON-RPC response dictionary (success or error)
    """
    request_id = request.get("id")

    # Validate request structure
    is_valid, error = validate_jsonrpc_request(request)
    if not is_valid:
        assert error is not None  # Type narrowing for mypy
        return create_jsonrpc_error(error["code"], error["message"], request_id)

    method = request["method"]
    params = request.get("params") or {}

    # Route to appropriate handler
    try:
        if method == "initialize":
            # MCP protocol handshake
            # TODO: Verify full MCP 2025-06-18 compatibility
            # - 2025-06-18 removed JSON-RPC batching support (breaking change)
            # - Added structured JSON tool output (structuredContent)
            # - Enhanced OAuth 2.0 integration with resource parameter (RFC 8707)
            # - Server-initiated user input via elicitation/create requests
            # Current implementation status: Updated version only, features pending audit
            # Story #22: Use configured service_display_name, fallback to "Neo"
            config = get_config_service().get_config()
            display_name = config.service_display_name or "Neo"
            result = {
                "protocolVersion": "2025-06-18",
                "capabilities": {"tools": {}},
                "serverInfo": {"name": display_name, "version": __version__},
            }
            return create_jsonrpc_response(result, request_id)
        elif method == "notifications/initialized":
            # Per losvedir: Must return 202, empty response
            # This is a notification from client, no response data needed
            # Return empty result (FastAPI will use 202 if we set it in route)
            return create_jsonrpc_response(None, request_id)
        elif method == "tools/list":
            result = handle_tools_list(params, user)
            return create_jsonrpc_response(result, request_id)
        elif method == "prompts/list":
            # Per losvedir line 97-106 and README line 275
            # Claude always requests this regardless of capabilities
            result = {"prompts": []}
            return create_jsonrpc_response(result, request_id)
        elif method == "resources/list":
            # Per losvedir line 108-117 and README line 275
            # Claude always requests this regardless of capabilities
            result = {"resources": []}
            return create_jsonrpc_response(result, request_id)
        elif method == "tools/call":
            result = await handle_tools_call(params, user, session_id=session_id)
            return create_jsonrpc_response(result, request_id)
        else:
            return create_jsonrpc_error(
                -32601, f"Method not found: {method}", request_id
            )
    except ValueError as e:
        # Invalid params error
        return create_jsonrpc_error(-32602, f"Invalid params: {str(e)}", request_id)
    except Exception as e:
        # Internal error
        return create_jsonrpc_error(
            -32603,
            f"Internal error: {str(e)}",
            request_id,
            data={"exception_type": type(e).__name__},
        )


async def process_batch_request(
    batch: List[Dict[str, Any]], user: User, session_id: Optional[str] = None
) -> List[Dict[str, Any]]:
    """
    Process a batch of JSON-RPC 2.0 requests.

    Args:
        batch: List of JSON-RPC request dictionaries
        user: Authenticated user
        session_id: Optional MCP session ID for session state management

    Returns:
        List of JSON-RPC response dictionaries
    """
    responses = []

    for request in batch:
        response = await process_jsonrpc_request(request, user, session_id=session_id)
        responses.append(response)

    return responses


@mcp_router.post("/mcp")
async def mcp_endpoint(
    request: Request,
    response: Response,
    current_user: User = Depends(get_current_user_for_mcp),
) -> Union[Dict[str, Any], List[Dict[str, Any]]]:
    """
    MCP JSON-RPC 2.0 endpoint.

    Handles tool discovery and execution via JSON-RPC 2.0 protocol.
    Supports both single requests and batch requests.

    Authentication priority (Story #616 AC6):
    1. MCP credentials (Basic auth or client_secret_post)
    2. OAuth/JWT tokens (existing authentication)
    3. 401 Unauthorized if none present

    Args:
        request: FastAPI Request object
        response: FastAPI Response object for setting headers
        current_user: Authenticated user (from MCP credentials, OAuth, or JWT)

    Returns:
        JSON-RPC response (single or batch)
    """
    # Bug fix: Use session_id from query parameter if provided (for session persistence)
    # Otherwise generate a new one for new sessions
    session_id = request.query_params.get("session_id")
    if not session_id:
        session_id = str(uuid.uuid4())
    response.headers["Mcp-Session-Id"] = session_id

    try:
        # Sliding expiration for cookie-authenticated sessions only (no Bearer header)
        if "authorization" not in request.headers:
            token = request.cookies.get("cidx_session")
            if token and auth_deps.jwt_manager is not None:
                try:
                    payload = auth_deps.jwt_manager.validate_token(token)
                    if _should_refresh_token(payload):
                        _refresh_jwt_cookie(response, payload)
                except Exception:
                    # Ignore refresh errors; normal auth flow already enforced
                    pass

        body = await request.json()
    except Exception:
        # Parse error - return JSON-RPC error
        return create_jsonrpc_error(-32700, "Parse error: Invalid JSON", None)

    # Check if batch request (array) or single request (object)
    if isinstance(body, list):
        return await process_batch_request(body, current_user, session_id=session_id)
    elif isinstance(body, dict):
        return await process_jsonrpc_request(body, current_user, session_id=session_id)
    else:
        return create_jsonrpc_error(
            -32600, "Invalid Request: body must be object or array", None
        )


async def sse_event_generator():
    """Generate minimal SSE events."""
    yield {"data": "connected"}


def get_optional_user(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> Optional[User]:
    """
    Optional user dependency that returns None for unauthenticated requests.

    Wraps get_current_user() to handle authentication failures gracefully
    instead of raising HTTPException.

    Used for endpoints that need to distinguish between authenticated
    and unauthenticated requests (e.g., MCP SSE endpoint per RFC 9728).

    Args:
        request: HTTP request object for cookie extraction
        credentials: Bearer token from Authorization header

    Returns:
        User object if authentication succeeds, None otherwise
    """
    from fastapi import HTTPException

    try:
        return get_current_user(request, credentials)
    except HTTPException:
        # Authentication failed - return None to indicate unauthenticated
        return None


@mcp_router.get("/mcp", response_model=None)
async def mcp_sse_endpoint(
    user: Optional[User] = Depends(get_optional_user),
) -> Union[Response, EventSourceResponse]:
    """
    MCP SSE endpoint for server-to-client notifications.

    Per MCP specification (RFC 9728 Section 5):
    - Unauthenticated requests: Return HTTP 401 with WWW-Authenticate header
    - Authenticated requests: Return SSE stream with full MCP capabilities

    Args:
        user: Authenticated user (None if authentication fails)

    Returns:
        401 Response with WWW-Authenticate header for unauthenticated requests,
        SSE stream for authenticated requests
    """
    if user is None:
        # Per RFC 9728: Return 401 with WWW-Authenticate header for unauthenticated requests
        return Response(
            status_code=401,
            headers={
                "WWW-Authenticate": _build_www_authenticate_header(),
                "Content-Type": "application/json",
            },
            content='{"error": "unauthorized", "message": "Bearer token required for MCP access"}',
        )

    # Authenticated: return SSE stream with full MCP capabilities
    return EventSourceResponse(authenticated_sse_generator(user))


async def authenticated_sse_generator(user):
    """Full SSE stream for authenticated MCP clients."""
    # Send authenticated endpoint info
    yield {
        "event": "endpoint",
        "data": json.dumps(
            {
                "protocol": "mcp",
                "version": "2025-06-18",
                "capabilities": {"tools": {}},
                "user": user.username,
            }
        ),
    }

    # Full MCP notification stream
    while True:
        await asyncio.sleep(30)
        yield {"event": "ping", "data": "authenticated"}


@mcp_router.delete("/mcp")
def mcp_delete_session(
    current_user: User = Depends(get_current_user),
) -> Dict[str, str]:
    """MCP DELETE endpoint for session termination."""
    return {"status": "terminated"}


# === PUBLIC MCP ENDPOINT (No OAuth Challenge) ===


def get_optional_user_from_cookie(request: Request) -> Optional[User]:
    """Get user from JWT cookie if valid, None otherwise."""
    import logging
    from code_indexer.server.auth.dependencies import _validate_jwt_and_get_user

    token = request.cookies.get("cidx_session")
    if not token:
        return None

    try:
        return _validate_jwt_and_get_user(token)
    except Exception as e:
        logging.getLogger(__name__).debug(f"Cookie auth failed: {e}")
        return None


def handle_public_tools_list(user: Optional[User]) -> Dict[str, Any]:
    """Handle tools/list for /mcp-public endpoint."""
    if user is None:
        return {
            "tools": [
                {
                    "name": "authenticate",
                    "description": "Authenticate with API key to access CIDX tools",
                    "inputSchema": {
                        "type": "object",
                        "properties": {
                            "username": {"type": "string", "description": "Username"},
                            "api_key": {
                                "type": "string",
                                "description": "API key (cidx_sk_...)",
                            },
                        },
                        "required": ["username", "api_key"],
                    },
                }
            ]
        }
    from .tools import filter_tools_by_role

    # Story #185: Pass config to filter tools based on configuration requirements
    config = get_config_service().get_config()
    return {"tools": filter_tools_by_role(user, config=config)}


async def process_public_jsonrpc_request(
    request_data: Dict[str, Any],
    user: Optional[User],
    http_request: Request,
    http_response: Response,
    session_id: Optional[str] = None,
) -> Dict[str, Any]:
    """Process JSON-RPC request for /mcp-public endpoint."""
    request_id = request_data.get("id")

    is_valid, error = validate_jsonrpc_request(request_data)
    if not is_valid:
        assert error is not None
        return create_jsonrpc_error(error["code"], error["message"], request_id)

    method = request_data["method"]
    params = request_data.get("params") or {}

    if not isinstance(params, dict):
        return create_jsonrpc_error(
            -32602, "Invalid params: must be an object", request_id
        )

    try:
        if method == "initialize":
            # Story #22: Use configured service_display_name, fallback to "Neo"
            config = get_config_service().get_config()
            display_name = config.service_display_name or "Neo"
            return create_jsonrpc_response(
                {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": display_name, "version": __version__},
                },
                request_id,
            )

        elif method == "notifications/initialized":
            # Per losvedir: Must return 202, empty response
            # This is a notification from client, no response data needed
            return create_jsonrpc_response(None, request_id)

        elif method == "tools/list":
            result = handle_public_tools_list(user)
            return create_jsonrpc_response(result, request_id)

        elif method == "prompts/list":
            # Per losvedir line 97-106 and README line 275
            # Claude always requests this regardless of capabilities
            result = {"prompts": []}
            return create_jsonrpc_response(result, request_id)

        elif method == "resources/list":
            # Per losvedir line 108-117 and README line 275
            # Claude always requests this regardless of capabilities
            result = {"resources": []}
            return create_jsonrpc_response(result, request_id)

        elif method == "tools/call":
            tool_name = params.get("name")

            if tool_name == "authenticate":
                from .handlers import HANDLER_REGISTRY

                if "authenticate" not in HANDLER_REGISTRY:
                    return create_jsonrpc_error(
                        -32601, "authenticate tool not yet implemented", request_id
                    )
                handler = HANDLER_REGISTRY["authenticate"]
                result = await handler(
                    params.get("arguments", {}), http_request, http_response
                )
                return create_jsonrpc_response(result, request_id)

            if user is None:
                return create_jsonrpc_error(
                    -32602,
                    "Authentication required. Call authenticate tool first.",
                    request_id,
                )

            result = await handle_tools_call(params, user, session_id=session_id)
            return create_jsonrpc_response(result, request_id)

        else:
            return create_jsonrpc_error(
                -32601, f"Method not found: {method}", request_id
            )

    except ValueError as e:
        return create_jsonrpc_error(-32602, f"Invalid params: {str(e)}", request_id)
    except Exception as e:
        return create_jsonrpc_error(
            -32603,
            f"Internal error: {str(e)}",
            request_id,
            data={"exception_type": type(e).__name__},
        )


async def unauthenticated_sse_generator():
    """Minimal SSE stream for unauthenticated /mcp-public clients."""
    yield {
        "event": "endpoint",
        "data": json.dumps(
            {
                "protocol": "mcp",
                "version": "2025-06-18",
                "capabilities": {"tools": {}},
                "authenticated": False,
            }
        ),
    }
    while True:
        await asyncio.sleep(30)
        yield {"event": "ping", "data": "unauthenticated"}


@mcp_router.post("/mcp-public")
async def mcp_public_endpoint(
    request: Request, response: Response
) -> Union[Dict[str, Any], List[Dict[str, Any]]]:
    """Public MCP endpoint (no OAuth challenge)."""
    session_id = str(uuid.uuid4())
    response.headers["Mcp-Session-Id"] = session_id
    # Sliding expiration for cookie-authenticated sessions
    token = request.cookies.get("cidx_session")
    if token and auth_deps.jwt_manager is not None:
        try:
            payload = auth_deps.jwt_manager.validate_token(token)
            if _should_refresh_token(payload):
                _refresh_jwt_cookie(response, payload)
        except Exception:
            pass

    user = get_optional_user_from_cookie(request)

    try:
        body = await request.json()
    except Exception:
        return create_jsonrpc_error(-32700, "Parse error: Invalid JSON", None)

    if isinstance(body, list):
        return [
            await process_public_jsonrpc_request(
                req, user, request, response, session_id=session_id
            )
            for req in body
        ]
    elif isinstance(body, dict):
        return await process_public_jsonrpc_request(
            body, user, request, response, session_id=session_id
        )
    else:
        return create_jsonrpc_error(
            -32600, "Invalid Request: body must be object or array", None
        )


@mcp_router.get("/mcp-public", response_model=None)
async def mcp_public_sse_endpoint(request: Request) -> EventSourceResponse:
    """Public MCP SSE endpoint (no OAuth challenge)."""
    user = get_optional_user_from_cookie(request)
    if user is None:
        return EventSourceResponse(unauthenticated_sse_generator())
    return EventSourceResponse(authenticated_sse_generator(user))
