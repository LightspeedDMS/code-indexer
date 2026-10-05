"""Per-request audit attribution context.

Every audit event records the front door a request entered through
(``source``), the immediate peer address (``client_ip``) and, when known,
how the caller authenticated (``auth_method``).  That information lives in a
MUTABLE holder placed in a ``ContextVar`` by ``AuditRequestContextMiddleware``
for the lifetime of one request.

Why a mutable holder: FastAPI runs sync dependencies in a worker thread with
a COPIED context, so a ``ContextVar.set()`` performed inside a dependency is
invisible to the handler.  Mutating a field of the holder object that the
middleware already placed in the var is visible everywhere the request's
context was copied to.  Code other than the middleware must never re-set the
var; it may only mutate the holder's fields.

The holder is per-request state carried by the request's own context; it is
never shared across requests or nodes.

Importing this module must stay cheap: it imports neither starlette nor
fastapi (pure ASGI).
"""

from __future__ import annotations

from contextvars import ContextVar, Token
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, MutableMapping, Optional

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

SOURCE_REST = "rest"
SOURCE_MCP = "mcp"
SOURCE_WEB = "web"
SOURCE_SYSTEM = "system"

AUTH_METHOD_WEB_SESSION = "web_session"
AUTH_METHOD_JWT = "jwt"
AUTH_METHOD_OAUTH_TOKEN = "oauth_token"
AUTH_METHOD_MCP_CREDENTIAL = "mcp_credential"

# Path prefixes served by the Web UI routers (server-rendered pages and
# their form posts).  Everything else reached over HTTP is the REST door,
# except the MCP endpoints.
_WEB_PATH_PREFIXES = ("/admin/", "/user/", "/login/")
_WEB_EXACT_PATHS = frozenset({"/login", "/admin", "/user"})
_MCP_PATH_PREFIX = "/mcp"


@dataclass
class AuditRequestContext:
    """Mutable per-request attribution holder (see module docstring)."""

    source: str
    client_ip: Optional[str] = None
    auth_method: Optional[str] = None
    # Set by the MCP dispatcher for each tool call made while the session
    # impersonates another user: the authenticated administrator (recorded as
    # the actor) and the impersonated user (recorded as the subject).
    authenticated_actor: Optional[str] = None
    impersonated_user: Optional[str] = None


_audit_request_context: ContextVar[Optional[AuditRequestContext]] = ContextVar(
    "cidx_audit_request_context", default=None
)


def current_audit_request_context() -> Optional[AuditRequestContext]:
    """Return the holder bound to the current request, or None outside one."""
    return _audit_request_context.get()


def note_auth_method(method: str) -> None:
    """Record how the current request's caller authenticated.

    Called by the authentication dependency branch that succeeded.  Mutates
    the request's holder (never re-sets the var); outside a request it does
    nothing.
    """
    ctx = _audit_request_context.get()
    if ctx is not None:
        ctx.auth_method = method


def note_mcp_principal(
    authenticated_actor: str, impersonated_user: Optional[str]
) -> None:
    """Record who acts in the current MCP tool call.

    Called by the MCP dispatcher before every tool call.  With
    *impersonated_user* set, audit events built during the call name
    *authenticated_actor* as the actor and *impersonated_user* as the
    subject; with it None, any earlier call's impersonation is cleared (a
    JSON-RPC batch shares one holder).  Mutates the request's holder (never
    re-sets the var); outside a request it does nothing.
    """
    ctx = _audit_request_context.get()
    if ctx is None:
        return
    if impersonated_user is None:
        ctx.authenticated_actor = None
        ctx.impersonated_user = None
        return
    ctx.authenticated_actor = authenticated_actor
    ctx.impersonated_user = impersonated_user


def bind_audit_request_context(
    ctx: AuditRequestContext,
) -> Token[Optional[AuditRequestContext]]:
    """Place *ctx* in the context var; return the token for the reset."""
    return _audit_request_context.set(ctx)


def reset_audit_request_context(
    token: Token[Optional[AuditRequestContext]],
) -> None:
    """Undo a previous :func:`bind_audit_request_context`."""
    _audit_request_context.reset(token)


def classify_source(path: str) -> str:
    """Map a request path to the front door it entered through.

    ``/mcp`` and anything under it (``/mcp-public`` included) is MCP; the
    Web UI paths are ``web``; every other HTTP path is ``rest``.
    """
    if path.startswith(_MCP_PATH_PREFIX):
        return SOURCE_MCP
    if path in _WEB_EXACT_PATHS or path.startswith(_WEB_PATH_PREFIXES):
        return SOURCE_WEB
    return SOURCE_REST


def build_request_context(path: str, client_ip: Optional[str]) -> AuditRequestContext:
    """Build the holder for one HTTP request.

    Web requests are authenticated by the session cookie, so their
    ``auth_method`` is known up front; for the other doors the auth
    dependency that authenticates the caller fills it in.
    """
    source = classify_source(path)
    auth_method = AUTH_METHOD_WEB_SESSION if source == SOURCE_WEB else None
    return AuditRequestContext(
        source=source, client_ip=client_ip, auth_method=auth_method
    )


class AuditRequestContextMiddleware:
    """Pure-ASGI middleware binding an :class:`AuditRequestContext` per request."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return
        client = scope.get("client")
        client_ip = client[0] if client else None
        ctx = build_request_context(str(scope.get("path", "")), client_ip)
        token = bind_audit_request_context(ctx)
        try:
            await self.app(scope, receive, send)
        finally:
            reset_audit_request_context(token)
