"""Authenticated API documentation routes.

API documentation requires an authenticated session or token: GET /docs
(Swagger UI), GET /redoc (ReDoc) and GET /openapi.json accept either a valid
Web UI session cookie or a valid bearer token. FastAPI's built-in, open
documentation routes are disabled in ``startup/app_wiring.py``
(``docs_url``/``redoc_url``/``openapi_url`` = None) and replaced by these.

Unauthenticated requests to the HTML pages are redirected to the login page
(same behaviour as the wiki pages); an unauthenticated request for the JSON
schema answers 401. The Swagger UI and ReDoc pages fetch ``/openapi.json``
same-origin, so the browser sends the session cookie with that fetch.
"""

from typing import Optional
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.openapi.docs import get_redoc_html, get_swagger_ui_html
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.security import HTTPAuthorizationCredentials
from starlette import status

from code_indexer.server.auth.dependencies import (
    get_current_user_web_or_api,
    security,
)
from code_indexer.server.auth.user_manager import User

OPENAPI_PATH = "/openapi.json"

api_docs_router = APIRouter(include_in_schema=False)


def _user_or_redirect_to_login(
    request: Request,
    credentials: Optional[HTTPAuthorizationCredentials] = Depends(security),
) -> User:
    """The shared cookie-or-token resolver; a 401 becomes a login redirect."""
    try:
        return get_current_user_web_or_api(request, credentials)
    except HTTPException as exc:
        if exc.status_code != status.HTTP_401_UNAUTHORIZED:
            raise
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER,
            headers={"Location": _login_location(request)},
        )


def _root_path(request: Request) -> str:
    """Reverse-proxy prefix of this request ("" when served at the root)."""
    return str(request.scope.get("root_path", "")).rstrip("/")


def _login_location(request: Request) -> str:
    """Login page URL that returns to this page, both inside the proxy prefix.

    Depending on the ASGI server, the scope path may or may not already carry
    root_path; the prefix is applied exactly once either way.
    """
    root_path = _root_path(request)
    path = request.url.path
    if root_path and not (path == root_path or path.startswith(root_path + "/")):
        path = root_path + path
    if request.url.query:
        path += f"?{request.url.query}"
    return f"{root_path}/login?redirect_to={quote(path)}"


def _openapi_url(request: Request) -> str:
    """Schema URL honouring a reverse-proxy root_path (as FastAPI's own docs do)."""
    return _root_path(request) + OPENAPI_PATH


@api_docs_router.get(OPENAPI_PATH)
def openapi_json(
    request: Request, _user: User = Depends(get_current_user_web_or_api)
) -> JSONResponse:
    """OpenAPI schema of the server API (authenticated).

    Like FastAPI's own schema route, a request served under a proxy prefix
    gets ``{"url": root_path}`` as the first ``servers`` entry, so Swagger's
    "Try it out" targets the prefixed paths. The entry is added to a copy
    per request; the app's cached schema is never mutated.
    """
    schema = request.app.openapi()
    root_path = _root_path(request)
    if root_path and request.app.root_path_in_servers:
        servers = schema.get("servers", [])
        if root_path not in {server.get("url") for server in servers}:
            schema = {**schema, "servers": [{"url": root_path}, *servers]}
    return JSONResponse(schema)


@api_docs_router.get("/docs")
def swagger_ui(
    request: Request, _user: User = Depends(_user_or_redirect_to_login)
) -> HTMLResponse:
    """Swagger UI for the server API (authenticated)."""
    return get_swagger_ui_html(
        openapi_url=_openapi_url(request),
        title=f"{request.app.title} - Swagger UI",
    )


@api_docs_router.get("/redoc")
def redoc(
    request: Request, _user: User = Depends(_user_or_redirect_to_login)
) -> HTMLResponse:
    """ReDoc for the server API (authenticated)."""
    return get_redoc_html(
        openapi_url=_openapi_url(request),
        title=f"{request.app.title} - ReDoc",
    )
