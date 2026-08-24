"""
Correlation ID context management and middleware for CIDX Server.

Implements correlation ID generation, storage in contextvars (async-safe),
and FastAPI middleware for automatic request/response correlation tracking.

Following Story #666 AC2: CorrelationContextMiddleware Implementation

Bug #1632: get_correlation_id/set_correlation_id/clear_correlation_id used
to read/write this module's OWN private ContextVar, which is only ever
populated by CorrelationContextMiddleware below -- and that middleware is
NEVER registered in startup/app_wiring.py (only
telemetry.correlation_bridge.CorrelationBridgeMiddleware is). That made
get_correlation_id() always return None in production for every one of
the ~70 files that import it from here (Story #1293 / Bug #1631 already
fixed 12 of those files individually by importing from the wired reader
directly; this bug heals the rest at the source instead).

The fix: these three functions now DELEGATE to
telemetry.correlation_bridge's canonical ContextVar, so every existing
`from code_indexer.server.middleware.correlation import get_correlation_id`
call site is healed without touching each file individually. Imports are
local to each function (not at module level) to preserve the Bug #1468
invariant that merely importing this module for the lightweight
contextvar helpers must not force fastapi/starlette to load --
telemetry.correlation_bridge imports starlette at module level.

CorrelationBridgeMiddleware is added via app.add_middleware() in
startup/app_wiring.py BEFORE any route is registered, and nothing is
added between it and route registration, so it always wraps
route/handler execution -- its ContextVar is guaranteed to be populated
by the time any handler, logging helper, or audit-logger call runs.
"""

from typing import Any, Optional

from .error_formatters import generate_correlation_id


def get_correlation_id() -> Optional[str]:
    """
    Get current correlation ID from context.

    Delegates to telemetry.correlation_bridge.get_current_correlation_id()
    (Bug #1632) -- the canonical ContextVar populated by the REGISTERED
    CorrelationBridgeMiddleware.

    Returns:
        Optional[str]: Current correlation ID or None if not set

    Example:
        >>> correlation_id = get_correlation_id()
        >>> if correlation_id:
        logger.error(
            format_error_log("APP-MIGRATE-001", "Error occurred", extra={"correlation_id": correlation_id}),
            extra=get_log_extra("APP-MIGRATE-001")
        )
    """
    from code_indexer.server.telemetry.correlation_bridge import (
        get_current_correlation_id,
    )

    # Explicit annotation (rather than a bare `return get_current_correlation_id()`)
    # works around a mypy module-identity quirk in this codebase's dual
    # code_indexer.*/src.code_indexer.* import-style setup, where the
    # cross-module call's return type otherwise resolves to Any.
    result: Optional[str] = get_current_correlation_id()
    return result


def set_correlation_id(correlation_id: str) -> None:
    """
    Set correlation ID in context.

    Delegates to telemetry.correlation_bridge.set_current_correlation_id()
    (Bug #1632) so this module's own CorrelationContextMiddleware (dead
    code -- never registered) and get_correlation_id() stay consistent
    with the one canonical store.

    Args:
        correlation_id: Correlation ID to store in context

    Example:
        >>> set_correlation_id("abc-123-def")
        >>> assert get_correlation_id() == "abc-123-def"
    """
    from code_indexer.server.telemetry.correlation_bridge import (
        set_current_correlation_id,
    )

    set_current_correlation_id(correlation_id)


def clear_correlation_id() -> None:
    """
    Clear correlation ID from context.

    Useful for test cleanup and explicit context clearing. Delegates to
    telemetry.correlation_bridge's ContextVar directly (Bug #1632) since
    that module exposes no public "clear" helper of its own.

    Example:
        >>> set_correlation_id("test-id")
        >>> clear_correlation_id()
        >>> assert get_correlation_id() is None
    """
    from code_indexer.server.telemetry import correlation_bridge

    correlation_bridge._correlation_id_var.set(None)


def __getattr__(name: str) -> Any:
    """PEP 562 lazy attribute resolution (Bug #1468).

    `CorrelationContextMiddleware` needs fastapi/starlette, but this
    module's lightweight `get_correlation_id`/`set_correlation_id`/
    `clear_correlation_id` functions do not -- and they are imported deep
    inside FilesystemVectorStore's import chain (via config_service.py),
    including pure CLI/solo usage with no fastapi need at all. Defining the
    class lazily here means merely importing this module for the
    contextvar helpers no longer forces fastapi/starlette to load.

    `CorrelationContextMiddleware` remains fully available via
    `from code_indexer.server.middleware.correlation import
    CorrelationContextMiddleware`, resolved (and cached in this module's
    globals, so subsequent accesses skip __getattr__ entirely) on first
    actual access.
    """
    if name == "CorrelationContextMiddleware":
        from fastapi import Request
        from starlette.middleware.base import BaseHTTPMiddleware

        class CorrelationContextMiddleware(BaseHTTPMiddleware):
            """
            FastAPI middleware for automatic correlation ID management.

            Features:
            - Extracts correlation ID from X-Correlation-ID request header
            - Generates UUID v4 if header not present
            - Stores correlation ID in contextvars (async-safe)
            - Adds X-Correlation-ID to response headers
            - Ensures correlation ID persists throughout request lifecycle

            Usage:
                >>> from fastapi import FastAPI
                >>> from code_indexer.server.middleware.correlation import CorrelationContextMiddleware
                >>>
                >>> app = FastAPI()
                >>> app.add_middleware(CorrelationContextMiddleware)

            Following Story #666 AC2 requirements:
            - Generate UUID v4 if X-Correlation-ID header not present ✓
            - Store correlation ID in contextvars (async-safe) ✓
            - Create get_correlation_id() helper function ✓
            - Add X-Correlation-ID to response headers ✓
            - Ensure middleware runs before all other processing ✓
            """

            async def dispatch(self, request: Request, call_next):
                """
                Process request and inject correlation ID.

                Args:
                    request: FastAPI request object
                    call_next: Next middleware/route handler

                Returns:
                    Response with X-Correlation-ID header
                """
                # Extract or generate correlation ID
                correlation_id = request.headers.get("X-Correlation-ID")
                if not correlation_id:
                    correlation_id = generate_correlation_id()

                # Store in context for request lifecycle
                set_correlation_id(correlation_id)

                # Process request
                response = await call_next(request)

                # Add correlation ID to response headers
                response.headers["X-Correlation-ID"] = correlation_id

                return response

        globals()["CorrelationContextMiddleware"] = CorrelationContextMiddleware
        return CorrelationContextMiddleware
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
