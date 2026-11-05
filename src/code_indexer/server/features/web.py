"""Consume ``routers()``, ``ui_pages()`` and ``config_section()`` on the web side.

* ``routers()`` -> included into the FastAPI app under the contribution's
  prefix, every route behind the core's authentication (bearer token, API
  key or the Web UI's session JWT; with ``auth=ROUTER_AUTH_HYBRID`` a Web UI
  session first, as the core's own browser-and-script routes) and the
  contribution's ``required_permission``; the caller is put on
  ``request.state`` (``cidx_sdk.web.PRINCIPAL_STATE_ATTR``). With
  ``login_redirect`` an unauthenticated request is sent to the login page
  instead of answered 401. A router's ``nav_label`` adds a nav entry
  linking to its prefix (see ``ui_pages()``). A router's ``static_dir`` is served without
  authentication at ``<prefix>/_static/``, mounted before the routers so no
  catch-all route of the feature takes those paths.
* ``ui_pages()`` -> included the same way but behind a Web UI session (no
  session: redirect to the login page, as every core page does). The page
  gets a renderer for its own templates inside the core shell
  (``cidx_sdk.web.RENDER_STATE_ATTR``); its static directory is served at
  ``/feature-static/<feature>/``; its nav entry is listed by the shell
  templates (``base.html`` / ``user_base.html`` call
  ``request.app.state.feature_nav(request)``) for sessions holding its
  permission.
* config sections -> ``GET /api/admin/features/config`` (schema and values)
  and ``PUT /api/admin/features/{feature}/config`` (runtime keys), both admin
  with elevation, and the Web UI config screen (``config.html`` lists
  ``request.app.state.feature_config_view(request)`` and posts to
  ``POST /admin/features/config/{feature}``). Mounted on every server, with
  or without features, so the door and its audit row are always the same.

A route that collides with a core route (same path and method) refuses
startup: a feature never shadows the core.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Set, Tuple

from cidx_sdk import (
    PRINCIPAL_STATE_ATTR,
    RENDER_STATE_ATTR,
    ROUTER_AUTH_HYBRID,
    FeatureLoadError,
    Principal,
    RouterContribution,
    UiPageContribution,
)
from fastapi import APIRouter, Body, Depends, FastAPI, Form, HTTPException, Request
from fastapi import Response, status
from fastapi.responses import RedirectResponse
from fastapi.routing import APIRoute
from fastapi.security import HTTPAuthorizationCredentials

from code_indexer.server.utils.route_table import iter_routes
from code_indexer.server.auth import dependencies
from code_indexer.server.auth.user_manager import User, UserRole, role_has_permission
from code_indexer.server.features import config as feature_config
from code_indexer.server.features.loader import LoadedFeature

logger = logging.getLogger(__name__)

#: URL prefix of a feature's static directory: ``/feature-static/<feature>``.
FEATURE_STATIC_PREFIX = "/feature-static"
#: Admin-shell and user-shell templates a feature page may extend.
ADMIN_SHELL = "base.html"
USER_SHELL = "user_base.html"


def _principal(user: User) -> Principal:
    role = getattr(user.role, "value", user.role)
    return Principal(username=user.username, role=str(role))


def _forbidden(permission: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_403_FORBIDDEN,
        detail=f"Insufficient permissions: {permission} required",
    )


def api_principal_dependency(
    required_permission: Optional[str],
) -> Callable[..., Principal]:
    """FastAPI dependency for a feature REST router: the core's API
    authentication, then *required_permission*, then the principal."""

    def _feature_api_principal(
        request: Request,
        user: User = Depends(dependencies.get_current_user),
    ) -> Principal:
        if not user.has_permission(required_permission):
            raise _forbidden(str(required_permission))
        principal = _principal(user)
        setattr(request.state, PRINCIPAL_STATE_ATTR, principal)
        return principal

    return _feature_api_principal


def _login_redirect(request: Request) -> HTTPException:
    """303 to the login page, back to the requested URL afterwards."""
    from urllib.parse import quote

    current_path = str(request.url.path)
    if request.url.query:
        current_path += f"?{request.url.query}"
    return HTTPException(
        status_code=status.HTTP_303_SEE_OTHER,
        headers={"Location": f"/login?redirect_to={quote(current_path)}"},
    )


def hybrid_principal_dependency(
    required_permission: Optional[str], login_redirect: bool
) -> Callable[..., Principal]:
    """FastAPI dependency for a feature router with ``auth=ROUTER_AUTH_HYBRID``:
    a Web UI session first, else a bearer token or API key (the core's
    hybrid authentication), then *required_permission*, then the principal.
    With *login_redirect* an unauthenticated request (401) becomes a 303 to
    the login page."""

    def _feature_hybrid_principal(
        request: Request,
        credentials: Optional[HTTPAuthorizationCredentials] = Depends(
            dependencies.security
        ),
    ) -> Principal:
        try:
            user = dependencies._hybrid_auth_impl(
                request, credentials, require_admin=False
            )
        except HTTPException as exc:
            if login_redirect and exc.status_code == status.HTTP_401_UNAUTHORIZED:
                raise _login_redirect(request) from exc
            raise
        if not user.has_permission(required_permission):
            raise _forbidden(str(required_permission))
        principal = _principal(user)
        setattr(request.state, PRINCIPAL_STATE_ATTR, principal)
        return principal

    return _feature_hybrid_principal


def _router_dependency(contribution: RouterContribution) -> Callable[..., Principal]:
    if contribution.auth == ROUTER_AUTH_HYBRID:
        return hybrid_principal_dependency(
            contribution.required_permission, contribution.login_redirect
        )
    return api_principal_dependency(contribution.required_permission)


def _session_user(request: Request, response: Response) -> User:
    """The Web UI session's live account; redirects to login without one."""
    from code_indexer.server.web.auth import require_user_session

    session = require_user_session(request, response)
    user: Optional[User] = request.app.state.user_manager.get_user(session.username)
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_303_SEE_OTHER, headers={"Location": "/login"}
        )
    return user


def _shell_environment() -> Any:
    """The core web shell's Jinja environment (its globals and templates).

    One place names where the shell keeps it, so the web-route split only
    has to keep this lookup working.
    """
    from code_indexer.server.web import routes as shell_routes

    return shell_routes.templates.env


def _page_templates(page: UiPageContribution) -> Any:
    """Templates for *page*: its own directories first, then the shell's,
    sharing the shell's globals (static version, server clock, ...)."""
    from fastapi.templating import Jinja2Templates
    from jinja2 import ChoiceLoader, FileSystemLoader

    shell = _shell_environment()
    loaders = [FileSystemLoader(str(d)) for d in page.template_dirs]
    env = shell.overlay(loader=ChoiceLoader([*loaders, shell.loader]))
    return Jinja2Templates(env=env)


def page_dependency(feature: str, page: UiPageContribution) -> Callable[..., Principal]:
    """FastAPI dependency for a feature page router: a Web UI session, the
    page's permission, the principal and the shell renderer."""
    templates_holder: List[Any] = []

    def _feature_page_principal(request: Request, response: Response) -> Principal:
        user = _session_user(request, response)
        if not user.has_permission(page.required_permission):
            raise _forbidden(str(page.required_permission))
        principal = _principal(user)
        if not templates_holder:
            templates_holder.append(_page_templates(page))
        templates = templates_holder[0]
        shell = ADMIN_SHELL if principal.role == "admin" else USER_SHELL

        def _render(template_name: str, context: Mapping[str, object]) -> object:
            values: Dict[str, object] = {
                "request": request,
                "show_nav": True,
                "current_page": page.path,
                "shell_template": shell,
                "page_title": page.title,
                "feature_name": feature,
                "principal": principal,
            }
            values.update(context)
            return templates.TemplateResponse(request, template_name, values)

        setattr(request.state, PRINCIPAL_STATE_ATTR, principal)
        setattr(request.state, RENDER_STATE_ATTR, _render)
        return principal

    return _feature_page_principal


def _existing_routes(app: FastAPI) -> Set[Tuple[str, str]]:
    found: Set[Tuple[str, str]] = set()
    for path, route in iter_routes(app.routes):
        methods = getattr(route, "methods", None) or {"*"}
        for method in methods:
            found.add((path, method))
    return found


def _check_router(
    feature: str, what: str, router: object, prefix: str, existing: Set[Tuple[str, str]]
) -> APIRouter:
    if not isinstance(router, APIRouter):
        raise FeatureLoadError(
            f"feature {feature!r} {what} router is a {type(router).__name__}, "
            "not a fastapi.APIRouter"
        )
    for path, route in iter_routes(router.routes, prefix):
        if not isinstance(route, APIRoute):
            continue
        for method in route.methods or ():
            if (path, method) in existing:
                raise FeatureLoadError(
                    f"feature {feature!r} {what} route {method} {path} collides "
                    "with an existing route"
                )
            existing.add((path, method))
    return router


def _page_paths(router: APIRouter) -> Set[str]:
    return {
        path
        for path, route in iter_routes(router.routes)
        if isinstance(route, APIRoute) and "GET" in (route.methods or ())
    }


class NavEntry:
    """One feature navigation entry."""

    def __init__(self, label: str, path: str, permission: Optional[str]) -> None:
        self.label = label
        self.path = path
        self.permission = permission


def _nav_for(entries: Sequence[NavEntry]) -> Callable[[Any], List[Dict[str, str]]]:
    """``feature_nav(request)`` for the shell templates: the entries the
    viewer's session may open. Reads the session only when an entry needs a
    permission, and only its role: permissions follow from the role, as the
    core's pages decide from ``session.role``, with no user-store read."""

    def feature_nav(request: Any) -> List[Dict[str, str]]:
        if not entries:
            return []
        role: Optional[UserRole] = None
        if any(entry.permission for entry in entries):
            role = _viewer_role(request)
        return [
            {"label": entry.label, "path": entry.path}
            for entry in entries
            if entry.permission is None
            or (role is not None and role_has_permission(role, entry.permission))
        ]

    return feature_nav


def _publish_nav(app: FastAPI, entries: Sequence[NavEntry]) -> None:
    """Add *entries* to the app's feature nav and publish it. Cumulative,
    as routes are: a second install (more features on the same app) adds
    its entries rather than replacing the first install's."""
    published: List[NavEntry] = getattr(app.state, "feature_nav_entries", [])
    paths = {entry.path for entry in published}
    published = [*published, *(entry for entry in entries if entry.path not in paths)]
    app.state.feature_nav_entries = published
    app.state.feature_nav = _nav_for(published)


def _viewer_role(request: Any) -> Optional[UserRole]:
    try:
        from code_indexer.server.web.auth import get_session_manager

        session = get_session_manager().get_session(request)
        return None if session is None else UserRole(session.role)
    except Exception as exc:  # nav never fails a page
        logger.warning("feature nav: viewer not resolved (%s)", type(exc).__name__)
        return None


#: Where a router's ``static_dir`` is served, under the router's prefix.
ROUTER_STATIC_SUFFIX = "/_static"


def _static_directory(feature: str, directory: str) -> str:
    if not Path(directory).is_dir():
        raise FeatureLoadError(
            f"feature {feature!r} static_dir {directory} is not a directory"
        )
    return directory


def _router_static_path(
    feature: str, contribution: RouterContribution, existing: Set[Tuple[str, str]]
) -> str:
    """``<prefix>/_static``, refused when a route already serves it."""
    path = contribution.prefix + ROUTER_STATIC_SUFFIX
    if any(
        route_path == path or route_path.startswith(path + "/")
        for route_path, _ in existing
    ):
        raise FeatureLoadError(
            f"feature {feature!r} static path {path} collides with an existing route"
        )
    existing.add((path, "*"))
    return path


def install_feature_web(app: FastAPI, loaded: Sequence[LoadedFeature]) -> None:
    """Include every feature router and page, mount static directories and
    publish the nav. Refuses (``FeatureLoadError``) before including anything
    when a router is not an ``APIRouter``, a route collides, or a page's
    router does not serve its path."""
    from fastapi.staticfiles import StaticFiles

    existing = _existing_routes(app)
    routers: List[Tuple[RouterContribution, APIRouter]] = []
    pages: List[Tuple[str, UiPageContribution, APIRouter]] = []
    statics: Dict[str, str] = {}
    router_statics: List[Tuple[str, str, str]] = []
    for item in loaded:
        name = item.manifest.name
        for contribution in item.contributions.routers:
            if contribution.static_dir is not None:
                router_statics.append(
                    (
                        name,
                        _router_static_path(name, contribution, existing),
                        _static_directory(name, contribution.static_dir),
                    )
                )
            routers.append(
                (
                    contribution,
                    _check_router(
                        name, "REST", contribution.router, contribution.prefix, existing
                    ),
                )
            )
        for page in item.contributions.ui_pages:
            router = _check_router(name, "page", page.router, "", existing)
            if page.path not in _page_paths(router):
                raise FeatureLoadError(
                    f"feature {name!r} page {page.path} is not served (GET) by its router"
                )
            pages.append((name, page, router))
            if page.static_dir is not None:
                statics[name] = _static_directory(name, page.static_dir)
    for name, path, directory in router_statics:
        app.mount(
            path,
            StaticFiles(directory=directory),
            name=f"feature-static-{name}{path.replace('/', '-')}",
        )
    for contribution, router in routers:
        app.include_router(
            router,
            prefix=contribution.prefix,
            dependencies=[Depends(_router_dependency(contribution))],
        )
    for name, page, router in pages:
        app.include_router(router, dependencies=[Depends(page_dependency(name, page))])
    for name, directory in statics.items():
        app.mount(
            f"{FEATURE_STATIC_PREFIX}/{name}",
            StaticFiles(directory=directory),
            name=f"feature-static-{name}",
        )
    publish_feature_nav(app, loaded)


def publish_feature_nav(app: FastAPI, loaded: Sequence[LoadedFeature]) -> None:
    """Publish the nav entries of *loaded*'s routers and pages on *app*
    (``install_feature_web`` does this after mounting them)."""
    nav: List[NavEntry] = []
    for item in loaded:
        nav.extend(
            NavEntry(
                contribution.nav_label, contribution.prefix, contribution.nav_permission
            )
            for contribution in item.contributions.routers
            if contribution.nav_label
        )
        nav.extend(
            NavEntry(page.nav_label, page.path, page.required_permission)
            for page in item.contributions.ui_pages
            if page.nav_label
        )
    _publish_nav(app, nav)


# ── config sections: admin API and Web UI ────────────────────────────────────


def _sections(request: Request) -> List[Tuple[str, Any]]:
    registry = getattr(request.app.state, "feature_registry", None)
    return [] if registry is None else registry.config_sections()


def _current_config() -> Any:
    from code_indexer.server.services.config_service import get_config_service

    return get_config_service().get_config()


def _commit_feature_settings(
    request: Request, feature: str, updates: Mapping[str, Any], actor: str
) -> List[Dict[str, Any]]:
    """Validate *updates* against *feature*'s section and commit them as one
    audited runtime change (``config_changed`` on
    ``feature_settings.<feature>``). Returns the described sections."""
    from code_indexer.server.services.config_service import get_config_service

    section = feature_config.find_section(_sections(request), feature)
    if section is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=f"feature {feature!r} has no config section on this server",
        )
    service = get_config_service()
    try:
        feature_config.commit_runtime_update(service, feature, section, updates, actor)
    except feature_config.FeatureConfigError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)
        ) from exc
    return feature_config.describe_sections(_sections(request), service.get_config())


def _config_view_for(app: FastAPI) -> Callable[[Any], Dict[str, Any]]:
    """``feature_config_view(request)`` for ``config.html``."""

    def feature_config_view(request: Any) -> Dict[str, Any]:
        registry = getattr(app.state, "feature_registry", None)
        sections = [] if registry is None else registry.config_sections()
        if not sections:
            return {"sections": [], "csrf_token": ""}
        from code_indexer.server.web.auth import get_session_manager

        session = get_session_manager().get_session(request)
        return {
            "sections": feature_config.describe_sections(sections, _current_config()),
            "csrf_token": "" if session is None else session.csrf_token,
        }

    return feature_config_view


def _form_value(raw: str, current: Any) -> Any:
    """A web form string as the type of the key's current value."""
    if isinstance(current, bool):
        return raw.strip().lower() in ("true", "1", "on", "yes")
    if isinstance(current, int):
        return int(raw)
    if isinstance(current, float):
        return float(raw)
    return raw


#: Form field prefix of a runtime key on the config screen.
FORM_FIELD_PREFIX = "setting__"


def _save_config_form(
    request: Request, feature: str, raw: Mapping[str, str], actor: str
) -> None:
    """Convert the config screen's form for *feature* and commit it (blocking).
    An unchecked checkbox is absent from the form: it means False."""
    section = feature_config.find_section(_sections(request), feature)
    if section is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND)
    values = feature_config.section_values(feature, section, _current_config())
    updates: Dict[str, Any] = {}
    for key in section.keys:
        if key.bootstrap:
            continue
        field_value = raw.get(FORM_FIELD_PREFIX + key.name)
        if isinstance(key.default, bool):
            updates[key.name] = field_value is not None
        elif field_value is not None:
            try:
                updates[key.name] = _form_value(field_value, values[key.name])
            except ValueError as exc:
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail=f"{feature}.{key.name}: {exc}",
                ) from exc
    _commit_feature_settings(request, feature, updates, actor)


def install_feature_config_routes(app: FastAPI) -> None:
    """The feature-settings doors (always mounted; empty without features)."""
    router = APIRouter()

    @router.get("/api/admin/features/config")
    def list_feature_config(
        request: Request,
        _admin: User = Depends(dependencies.require_elevation()),
    ) -> Dict[str, Any]:
        """Every loaded feature's config schema and current values (admin,
        elevation, as every admin REST read; the Web UI config screen reads
        them server-side through ``feature_config_view``)."""
        return {
            "sections": feature_config.describe_sections(
                _sections(request), _current_config()
            )
        }

    @router.put("/api/admin/features/{feature}/config")
    def update_feature_config(
        request: Request,
        feature: str,
        updates: Dict[str, Any] = Body(...),
        admin: User = Depends(dependencies.require_elevation()),
    ) -> Dict[str, Any]:
        """Set runtime keys of *feature*'s section (bootstrap keys refused)."""
        return {
            "sections": _commit_feature_settings(
                request, feature, updates, admin.username
            )
        }

    @router.post("/admin/features/config/{feature}")
    async def save_feature_config_form(
        request: Request,
        feature: str,
        csrf_token: Optional[str] = Form(None),
        admin: User = Depends(dependencies.require_elevation()),
    ) -> RedirectResponse:
        """The Web UI config screen's form for *feature*'s runtime keys."""
        import anyio

        from code_indexer.server.web.auth import get_session_manager

        valid, _reason = get_session_manager().validate_csrf_token(request, csrf_token)
        if not valid:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN, detail="Invalid CSRF token"
            )
        form = await request.form()
        raw = {key: value for key, value in form.items() if isinstance(value, str)}
        await anyio.to_thread.run_sync(
            _save_config_form, request, feature, raw, admin.username
        )
        return RedirectResponse(
            url="/admin/config#feature-settings",
            status_code=status.HTTP_303_SEE_OTHER,
        )

    app.include_router(router)
    app.state.feature_config_view = _config_view_for(app)
    _publish_nav(app, ())
