"""Web Audit Logs page (``/admin/audit-logs``), admin only.

A thin adapter over the one shared read path
(``services/audit_log_query.query_audit_log``): it maps query parameters to
filters, renders :class:`CanonicalAuditRow` as HTML, and never filters, pages
or counts on its own.

Access follows the other admin pages: the shell page needs an admin session;
every data route (row list, aggregate, export) additionally carries
``require_elevation()`` so the shared interceptor opens the TOTP modal when
the admin has no active elevation window.

Every route is a plain ``def`` (run in the threadpool), and the export
streams from a synchronous generator (iterated in the threadpool), so no
database read ever runs on the event loop.
"""

from __future__ import annotations

import csv
import io
import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterator, List, Optional, Tuple
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, Response, StreamingResponse

from code_indexer.server.auth import dependencies
from code_indexer.server.services.audit_events import (
    AUDIT_ACTION_CATALOG,
    AUDIT_TARGET_ID_TYPE,
    OUTCOMES,
    SYSTEM_ACTOR_PREFIX,
    SystemComponent,
)
from code_indexer.server.services.audit_log_query import (
    AUDIT_COUNT_CAP,
    AUDIT_ROW_FIELDS,
    AUDIT_SOURCES,
    AUTH_ACTIVITY_DEFAULT_WINDOW,
    PAIRING_PENDING,
    PAIRING_UNKNOWN,
    SECURITY_VIEW_DEFAULT_WINDOW,
    TIER_ALL,
    TIER_AUTH_ACTIVITY,
    TIER_SECURITY,
    AuditAggregate,
    AuditFilters,
    AuditPage,
    AuditQueryError,
    CanonicalAuditRow,
    build_filters,
    clamp_limit,
    decode_details,
    query_audit_log,
    row_fields,
)

from . import routes as _web_routes
from .routes import _create_login_redirect, templates

logger = logging.getLogger(__name__)

audit_log_web_router = APIRouter(tags=["audit-logs-web"])

ROWS_URL = "/admin/partials/audit-logs"
AGGREGATE_URL = "/admin/partials/audit-logs-aggregate"
EXPORT_URL = "/admin/audit-logs/export"

VIEWS = (TIER_SECURITY, TIER_AUTH_ACTIVITY, TIER_ALL)
WINDOW_PRESETS: Dict[str, timedelta] = {
    "1h": timedelta(hours=1),
    "24h": AUTH_ACTIVITY_DEFAULT_WINDOW,
    "7d": SECURITY_VIEW_DEFAULT_WINDOW,
    "30d": timedelta(days=30),
}
WINDOW_CUSTOM = "custom"
WINDOW_ALL = "all"
WINDOW_CHOICES = tuple(WINDOW_PRESETS) + (WINDOW_CUSTOM, WINDOW_ALL)
# Security (and the correlation "all" view) default to 7 days; the
# authentication-activity aggregate defaults to its 24 h bounded window.
DEFAULT_WINDOWS: Dict[str, str] = {
    TIER_SECURITY: "7d",
    TIER_ALL: "7d",
    TIER_AUTH_ACTIVITY: "24h",
}
WINDOW_LABELS = {
    "1h": "Last hour",
    "24h": "Last 24 hours",
    "7d": "Last 7 days",
    "30d": "Last 30 days",
    WINDOW_CUSTOM: "Custom range (UTC)",
    WINDOW_ALL: "All time",
}

DETAILS_PREVIEW_CHARS = 200
DETAILS_EMBED_MAX_CHARS = 2048
EXPORT_MAX_ROWS = AUDIT_COUNT_CAP
EXPORT_CHUNK_ROWS = 500
EXPORT_FORMATS = ("csv", "json")
_FORMULA_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
_FILTER_PARAMS = (
    "action_type",
    "actor",
    "target_type",
    "target_id",
    "outcome",
    "source",
    "ip_address",
    "correlation_id",
)
_OUTCOME_UNKNOWN_HINT = (
    "No final outcome was recorded: the action may or may not have taken "
    "effect. Check the target's current state."
)


@dataclass(frozen=True)
class AuditPageRequest:
    """One resolved page request: view, filters and the applied window."""

    view: str
    window: str
    filters: AuditFilters
    all_time: bool
    raw_filters: Dict[str, str]

    def window_params(self) -> Dict[str, str]:
        """The applied window, frozen to absolute UTC bounds for paging."""
        if self.all_time:
            return {"window": WINDOW_ALL}
        params = {"window": WINDOW_CUSTOM}
        if self.filters.date_from is not None:
            params["date_from"] = self.filters.date_from.isoformat()
        if self.filters.date_to is not None:
            params["date_to"] = self.filters.date_to.isoformat()
        return params

    def query_params(self, view: Optional[str] = None) -> Dict[str, str]:
        params = {"view": view or self.view, **self.window_params()}
        params.update(self.raw_filters)
        return params


def resolve_request(
    params: Dict[str, Any], *, default_view: str, now: datetime
) -> AuditPageRequest:
    """Validate the page's query parameters; raise AuditQueryError if bad."""
    view = (params.get("view") or default_view).strip()
    if view not in VIEWS:
        raise AuditQueryError(f"view must be one of {list(VIEWS)}")
    window = (params.get("window") or DEFAULT_WINDOWS[view]).strip()
    if window not in WINDOW_CHOICES:
        raise AuditQueryError(f"window must be one of {list(WINDOW_CHOICES)}")
    date_from: Any = None
    date_to: Any = None
    if window in WINDOW_PRESETS:
        date_from = now - WINDOW_PRESETS[window]
    elif window == WINDOW_CUSTOM:
        date_from, date_to = params.get("date_from"), params.get("date_to")
        if not (date_from or date_to):
            raise AuditQueryError("a custom window needs date_from or date_to")
    raw = {
        name: str(params[name]).strip()
        for name in _FILTER_PARAMS
        if params.get(name) and str(params[name]).strip()
    }
    filters = build_filters(date_from=date_from, date_to=date_to, **raw)
    return AuditPageRequest(
        view=view,
        window=window,
        filters=filters,
        all_time=window == WINDOW_ALL,
        raw_filters=raw,
    )


def _format_utc(value: Optional[str]) -> str:
    if not value:
        return "-"
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return value
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def _outcome_view(row: CanonicalAuditRow) -> Tuple[str, str, str]:
    """(label, css modifier, tooltip).  A lone attempted row is never
    shown as success or failure; a job-based row reads "submitted"."""
    if row.outcome == "attempted":
        if row.pairing_state == PAIRING_PENDING:
            return "in progress", "pending", ""
        if row.pairing_state == PAIRING_UNKNOWN:
            return "outcome unknown", "unknown", _OUTCOME_UNKNOWN_HINT
        return "attempted", "attempted", ""
    if row.outcome == "success" and row.submitted_only:
        return (
            "submitted",
            "submitted",
            "The job was submitted; see Jobs for its result.",
        )
    if row.outcome in OUTCOMES:
        return str(row.outcome), str(row.outcome), ""
    return "-", "none", ""


def _actor_view(row: CanonicalAuditRow) -> Dict[str, Any]:
    if row.actor_is_system:
        kind = "system"
    elif not row.actor_is_authenticated:
        kind = "attempted"
    elif row.admin_id.startswith(SYSTEM_ACTOR_PREFIX):
        kind = "not_system"
    else:
        kind = "human"
    # During MCP impersonation the actor is the administrator and this is
    # the impersonated user (the subject); None otherwise.
    return {
        "name": row.admin_id,
        "kind": kind,
        "impersonated_user": row.impersonated_user,
    }


def _details_view(raw: Optional[str]) -> Dict[str, Any]:
    decoded = decode_details(raw)
    text = json.dumps(decoded, sort_keys=True, ensure_ascii=False) if decoded else ""
    preview = text[:DETAILS_PREVIEW_CHARS]
    return {
        "preview": preview + ("..." if len(text) > DETAILS_PREVIEW_CHARS else ""),
        "full": text[:DETAILS_EMBED_MAX_CHARS],
        "expandable": len(text) > DETAILS_PREVIEW_CHARS,
        "truncated": len(text) > DETAILS_EMBED_MAX_CHARS,
    }


def row_view(row: CanonicalAuditRow) -> Dict[str, Any]:
    """Display fields of one row (autoescaped by Jinja when rendered)."""
    label, modifier, tooltip = _outcome_view(row)
    return {
        "id": row.id,
        "time": _format_utc(row.timestamp),
        "actor": _actor_view(row),
        "action_type": row.action_type,
        "target": f"{row.target_type}:{row.target_id}",
        "outcome_label": label,
        "outcome_class": modifier,
        "outcome_title": tooltip,
        "source": row.source,
        "ip_address": row.ip_address,
        "node_id": row.node_id,
        "correlation_id": row.correlation_id,
        "details": _details_view(row.details),
    }


def _actor_options() -> List[str]:
    """Current usernames plus the system components (bounded by user count)."""
    names = [f"{SYSTEM_ACTOR_PREFIX}{c.value}" for c in SystemComponent]
    manager = dependencies.user_manager
    if manager is not None:
        names = sorted(u.username for u in manager.get_all_users()) + names
    return names


def _audit_store(request: Request) -> Any:
    store = getattr(request.app.state, "audit_service", None)
    if store is None:
        logger.error("Audit Logs page: audit store is not configured on app.state")
        raise HTTPException(status_code=503, detail="Audit log store unavailable")
    return store


def _window_label(request_: AuditPageRequest) -> str:
    if request_.all_time:
        return "All time"
    start = request_.filters.date_from
    end = request_.filters.date_to
    start_text = _format_utc(start.isoformat()) if start else "the beginning"
    end_text = _format_utc(end.isoformat()) if end else "now"
    return f"{start_text} to {end_text}"


def _error_response(request: Request, message: str) -> HTMLResponse:
    """A refused filter: HTTP 400 with the reason, never a silent empty list."""
    return templates.TemplateResponse(
        request,
        "partials/audit_logs_table.html",
        {"request": request, "error": message},
        status_code=400,
    )


def _url(base: str, params: Dict[str, str]) -> str:
    return f"{base}?{urlencode(params)}"


def _page_size(params: Dict[str, Any]) -> Optional[int]:
    raw = params.get("limit")
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        raise AuditQueryError("limit must be an integer") from None


_SHELL_PARAMS = ("view", "window", "date_from", "date_to") + _FILTER_PARAMS


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------


@audit_log_web_router.get("/audit-logs", response_class=HTMLResponse)
def audit_logs_page(request: Request) -> Response:
    """Shell page: filters, view switch and an htmx-loaded result section.

    Needs an admin session only; the rows arrive through the elevated
    partials.  Query parameters (a bookmarked or no-script form submission)
    are passed to the first partial load unchanged.
    """
    # Looked up on every call (never bound at import): the admin gate is
    # whatever web.routes currently provides.
    session = _web_routes._require_admin_session(request)
    if not session:
        return _create_login_redirect(request)
    passthrough = {
        name: request.query_params[name]
        for name in _SHELL_PARAMS
        if request.query_params.get(name)
    }
    view = passthrough.get("view", TIER_SECURITY)
    base = AGGREGATE_URL if view == TIER_AUTH_ACTIVITY else ROWS_URL
    config = {
        "rows_url": ROWS_URL,
        "aggregate_url": AGGREGATE_URL,
        "export_url": EXPORT_URL,
        "default_windows": DEFAULT_WINDOWS,
    }
    return templates.TemplateResponse(
        request,
        "audit_logs.html",
        {
            "request": request,
            "username": session.username,
            "current_page": "audit-logs",
            "show_nav": True,
            "initial_url": _url(base, passthrough) if passthrough else base,
            "selected": passthrough,
            "view": view,
            "action_types": sorted(AUDIT_ACTION_CATALOG),
            "target_types": sorted(AUDIT_TARGET_ID_TYPE),
            "outcomes": sorted(OUTCOMES),
            "sources": sorted(AUDIT_SOURCES),
            "windows": [(w, WINDOW_LABELS[w]) for w in WINDOW_CHOICES],
            # An invalid view is reported by the partial (HTTP 400); the
            # selector just shows the Security default meanwhile.
            "selected_window": passthrough.get("window")
            or DEFAULT_WINDOWS.get(view, DEFAULT_WINDOWS[TIER_SECURITY]),
            "page_config": config,
        },
    )


@audit_log_web_router.get(
    "/partials/audit-logs",
    response_class=HTMLResponse,
    dependencies=[Depends(dependencies.require_elevation())],
)
def audit_logs_rows_partial(request: Request) -> Response:
    """Raw rows for the current filters, one keyset page (Newer / Older)."""
    # Looked up on every call (never bound at import): the admin gate is
    # whatever web.routes currently provides.
    session = _web_routes._require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)
    params = dict(request.query_params)
    now = datetime.now(timezone.utc)
    try:
        resolved = resolve_request(params, default_view=TIER_SECURITY, now=now)
        page = query_audit_log(
            _audit_store(request),
            resolved.filters,
            tier=resolved.view,
            cursor=params.get("cursor") or None,
            direction=params.get("direction") or "older",
            limit=_page_size(params),
            now=now,
        )
    except AuditQueryError as exc:
        return _error_response(request, str(exc))
    assert isinstance(page, AuditPage)
    return templates.TemplateResponse(
        request,
        "partials/audit_logs_table.html",
        _rows_context(request, resolved, page),
    )


def _rows_context(
    request: Request, resolved: AuditPageRequest, page: AuditPage
) -> Dict[str, Any]:
    # The export covers the whole filtered set (bounded by the export
    # limit), never just this page, so it gets no page size.
    export_query = urlencode(resolved.query_params())
    base = resolved.query_params()
    page_size = _page_size(dict(request.query_params))
    if page_size is not None:
        base["limit"] = str(clamp_limit(page_size))
    on_cursor_page = bool(request.query_params.get("cursor"))
    return {
        "request": request,
        "error": None,
        "view": resolved.view,
        "rows": [row_view(row) for row in page.rows],
        "total": page.total,
        "total_capped": page.total_capped,
        "window_label": _window_label(resolved),
        "older_url": (
            _url(ROWS_URL, {**base, "cursor": page.next_cursor, "direction": "older"})
            if page.next_cursor
            else None
        ),
        "newer_url": (
            _url(ROWS_URL, {**base, "cursor": page.prev_cursor, "direction": "newer"})
            if page.has_newer and page.prev_cursor
            else None
        ),
        "latest_url": _url(ROWS_URL, base) if on_cursor_page else None,
        "export_query": export_query,
        "actor_options": _actor_options(),
    }


@audit_log_web_router.get(
    "/partials/audit-logs-aggregate",
    response_class=HTMLResponse,
    dependencies=[Depends(dependencies.require_elevation())],
)
def audit_logs_aggregate_partial(request: Request) -> Response:
    """Authentication activity grouped by (action, outcome) over a window."""
    # Looked up on every call (never bound at import): the admin gate is
    # whatever web.routes currently provides.
    session = _web_routes._require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)
    params = {**dict(request.query_params), "view": TIER_AUTH_ACTIVITY}
    now = datetime.now(timezone.utc)
    try:
        resolved = resolve_request(params, default_view=TIER_AUTH_ACTIVITY, now=now)
        result = query_audit_log(
            _audit_store(request),
            resolved.filters,
            tier=TIER_AUTH_ACTIVITY,
            aggregate=True,
            all_time=resolved.all_time,
            now=now,
        )
    except AuditQueryError as exc:
        return _error_response(request, str(exc))
    assert isinstance(result, AuditAggregate)
    base = resolved.query_params()
    groups = []
    for group in result.groups:
        drill = {**base, "action_type": group.action_type}
        if group.outcome is not None:
            drill["outcome"] = group.outcome
        groups.append(
            {
                "action_type": group.action_type,
                "outcome": group.outcome,
                "count": group.count,
                "distinct_actors": group.distinct_actors,
                "distinct_ips": group.distinct_ips,
                "first_seen": _format_utc(group.first_seen),
                "last_seen": _format_utc(group.last_seen),
                "drill_url": _url(ROWS_URL, drill),
            }
        )
    return templates.TemplateResponse(
        request,
        "partials/audit_logs_aggregate.html",
        {
            "request": request,
            "groups": groups,
            "truncated": result.truncated,
            "window_label": _window_label(resolved),
            "export_query": urlencode(base),
            "actor_options": _actor_options(),
        },
    )


# ---------------------------------------------------------------------------
# Export: the filtered rows, bounded and streamed
# ---------------------------------------------------------------------------

# The export writes exactly the fields REST and MCP expose for a row, with
# ``details`` already reduced to its allowlisted fields by the read path.
EXPORT_COLUMNS = AUDIT_ROW_FIELDS


def _export_record(row: CanonicalAuditRow) -> Dict[str, Any]:
    return row_fields(row)


def _csv_cell(value: Any) -> Any:
    """A spreadsheet must never evaluate an exported value as a formula."""
    if isinstance(value, str) and value.startswith(_FORMULA_PREFIXES):
        return "'" + value
    return value


def _export_pages(
    store: Any, resolved: AuditPageRequest
) -> Iterator[CanonicalAuditRow]:
    """Newest first, at most EXPORT_MAX_ROWS rows, one bounded page at a time."""
    cursor: Optional[str] = None
    exported = 0
    max_pages = -(-EXPORT_MAX_ROWS // EXPORT_CHUNK_ROWS)
    for _ in range(max_pages):
        remaining = EXPORT_MAX_ROWS - exported
        if remaining <= 0:
            return
        page = query_audit_log(
            store,
            resolved.filters,
            tier=resolved.view,
            cursor=cursor,
            limit=min(EXPORT_CHUNK_ROWS, remaining),
            with_total=False,
        )
        for row in page.rows:
            exported += 1
            yield row
        if page.next_cursor is None:
            return
        cursor = page.next_cursor


def _csv_stream(rows: Iterator[CanonicalAuditRow]) -> Iterator[str]:
    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(EXPORT_COLUMNS)
    for row in rows:
        writer.writerow([_csv_cell(v) for v in _export_record(row).values()])
        if buffer.tell() >= 64 * 1024:
            yield buffer.getvalue()
            buffer.seek(0)
            buffer.truncate()
    yield buffer.getvalue()


def _json_stream(
    rows: Iterator[CanonicalAuditRow], resolved: AuditPageRequest, exported_at: str
) -> Iterator[str]:
    header = {
        "exported_at": exported_at,
        "view": resolved.view,
        "window": _window_label(resolved),
        "filters": resolved.raw_filters,
        "max_rows": EXPORT_MAX_ROWS,
    }
    yield json.dumps(header)[:-1] + ', "rows": ['
    count = 0
    for row in rows:
        yield ("," if count else "") + json.dumps(_export_record(row))
        count += 1
    yield (
        f'], "row_count": {count}, "row_limit_reached": '
        + json.dumps(count >= EXPORT_MAX_ROWS)
        + "}"
    )


@audit_log_web_router.get(
    "/audit-logs/export",
    dependencies=[Depends(dependencies.require_elevation())],
)
def audit_logs_export(request: Request) -> Response:
    """Download the currently filtered rows as CSV or JSON (admin only).

    Bounded to EXPORT_MAX_ROWS rows, read through the shared function one
    keyset page at a time and streamed, so a large table is never loaded
    into memory.  Filters are validated before anything is streamed.
    """
    # Looked up on every call (never bound at import): the admin gate is
    # whatever web.routes currently provides.
    session = _web_routes._require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)
    params = dict(request.query_params)
    export_format = params.get("format", "csv")
    if export_format not in EXPORT_FORMATS:
        raise HTTPException(status_code=400, detail="format must be csv or json")
    now = datetime.now(timezone.utc)
    try:
        resolved = resolve_request(params, default_view=TIER_SECURITY, now=now)
    except AuditQueryError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    rows = _export_pages(_audit_store(request), resolved)
    stamp = now.strftime("%Y%m%d_%H%M%S")
    headers = {
        "Content-Disposition": f'attachment; filename="audit_logs_{stamp}.{export_format}"',
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    }
    logger.info(
        "Audit log export by %s (view=%s, format=%s)",
        session.username,
        resolved.view,
        export_format,
    )
    if export_format == "json":
        return StreamingResponse(
            _json_stream(rows, resolved, now.isoformat()),
            media_type="application/json",
            headers=headers,
        )
    return StreamingResponse(
        _csv_stream(rows), media_type="text/csv; charset=utf-8", headers=headers
    )
