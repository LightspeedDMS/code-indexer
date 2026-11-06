"""
Web Admin UI Module.

Provides web-based administration interface for CIDX server.

The four page routers are loaded on first access rather than at package import:
importing any ``code_indexer.server.web`` submodule (``web.auth``,
``web.pages._shared``, ...) would otherwise import every web page module, and
those import ``auth`` and ``routers`` modules that import ``web`` back.
"""

from typing import TYPE_CHECKING, Any

from .auth import (
    SessionManager,
    SessionData,
    get_session_manager,
    init_session_manager,
    require_admin_session,
    require_user_session,
    SESSION_COOKIE_NAME,
    SESSION_TIMEOUT_SECONDS,
)

if TYPE_CHECKING:
    from .routes import web_router, user_router, login_router, api_router

_LAZY_ROUTERS = frozenset({"web_router", "user_router", "login_router", "api_router"})


def __getattr__(name: str) -> Any:
    if name in _LAZY_ROUTERS:
        from . import routes

        return getattr(routes, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "web_router",
    "user_router",
    "login_router",
    "api_router",
    "SessionManager",
    "SessionData",
    "get_session_manager",
    "init_session_manager",
    "require_admin_session",
    "require_user_session",
    "SESSION_COOKIE_NAME",
    "SESSION_TIMEOUT_SECONDS",
]
