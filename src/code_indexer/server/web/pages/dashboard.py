"""
Admin dashboard page and its partials.

Moved out of ``web/routes.py`` (modular phase 6); see ``web/routes.py``.
"""

import socket
import time
from typing import Any, Dict, List, cast

from fastapi import APIRouter, Depends, Query, Request, HTTPException, status
from fastapi.responses import HTMLResponse, JSONResponse
from ...auth import dependencies
from ..auth import get_session_manager
from code_indexer import __version__ as cidx_version
from code_indexer.server.logging_utils import format_error_log

from ._shared import (
    _create_login_redirect,
    _get_background_job_manager,
    _require_admin_session,
    logger,
    templates,
)
from .config_values import (
    _get_qec_total_entries,
)

web_router = APIRouter()

# Story #674: Default time window (seconds) for per-user API breakdown card
_DEFAULT_PER_USER_API_FILTER_SECONDS = 86_400  # 24 hours


def _get_dashboard_service():
    """Get dashboard service, handling import lazily to avoid circular imports."""
    from ...services.dashboard_service import dashboard_service

    return dashboard_service


def _get_server_time() -> str:
    """
    Get current server time for template context (Story #89).

    Returns current UTC time in ISO 8601 format for server clock initialization.

    Returns:
        ISO 8601 formatted timestamp string (e.g., "2026-02-04T14:32:15.123456Z")
    """
    from datetime import datetime, timezone as tz

    current_time = datetime.now(tz.utc)
    return current_time.isoformat().replace("+00:00", "Z")


@web_router.get("/", response_class=HTMLResponse)
def dashboard(request: Request):
    """
    Dashboard page - main admin landing page.

    Requires authenticated admin session.
    Displays system health, job statistics, repository counts, and recent activity.
    """
    session_manager = get_session_manager()
    session = session_manager.get_session(request)

    if not session:
        # Not authenticated - redirect to unified login
        return _create_login_redirect(request)

    if session.role != "admin":
        # Not admin - forbidden
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Admin access required",
        )

    # Get aggregated dashboard data (Bug #671: Pass user role to show all repos for admins)
    dashboard_service = _get_dashboard_service()
    dashboard_data = dashboard_service.get_dashboard_data(
        session.username, session.role
    )

    # Story #30 AC5: Health section is now lazy-loaded via HTMX
    # No need to fetch database_health here - it will be loaded
    # asynchronously via /admin/partials/dashboard-health endpoint

    return templates.TemplateResponse(
        request,
        "dashboard.html",
        {
            "request": request,
            "username": session.username,
            "current_page": "dashboard",
            "show_nav": True,
            "server_time": _get_server_time(),
            "health": dashboard_data.health,
            "job_counts": dashboard_data.job_counts,
            "repo_counts": dashboard_data.repo_counts,
            "recent_jobs": dashboard_data.recent_jobs,
        },
    )


@web_router.get("/partials/dashboard-health", response_class=HTMLResponse)
def dashboard_health_partial(request: Request):
    """
    Partial refresh endpoint for dashboard health section.

    Returns HTML fragment for htmx partial updates.
    Story #712 AC1-AC4: Includes database health honeycomb data.
    Story #30 AC6: Uses cached database health (60s TTL) while
    system metrics (CPU, memory, disk, network) remain real-time.
    """
    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    dashboard_service = _get_dashboard_service()
    # System metrics are always real-time (AC3)
    health_data = dashboard_service.get_health_partial()

    # Story #30 AC6: Use cached database health (60s TTL)
    # Story #30 Bug Fix: Use singleton to ensure cache is shared across requests.
    # Previously, creating new DatabaseHealthService() on each request meant the
    # instance-level cache was always empty.
    # Story #505: Pass storage_mode so postgres mode skips migrated SQLite DBs
    # and shows PostgreSQL health instead.
    from ...services.database_health_service import get_database_health_service

    _storage_mode = getattr(request.app.state, "storage_mode", "sqlite")
    db_health_service = get_database_health_service(storage_mode=_storage_mode)
    database_health = db_health_service.get_all_database_health_cached()

    # Story #492: Read node metrics from DB for cluster carousel (AC3)
    # Use the backend stored in app.state during startup (set by lifespan.py).
    # In postgres/cluster mode this is NodeMetricsPostgresBackend; in standalone
    # mode it is NodeMetricsSqliteBackend.  Both satisfy NodeMetricsBackend Protocol.
    import json as _json

    node_metrics = []
    try:
        _nm_backend = getattr(request.app.state, "node_metrics_backend", None)
        if _nm_backend is not None:
            raw_metrics = _nm_backend.get_latest_per_node()
            # BUG 3 fix: pre-parse volumes_json so the template can iterate nm.volumes
            for row in raw_metrics:
                try:
                    row["volumes"] = _json.loads(row.get("volumes_json") or "[]")
                except (ValueError, TypeError):
                    row["volumes"] = []
            node_metrics = raw_metrics
    except Exception as e:
        logger.debug(f"Could not read node_metrics from DB: {e}")

    # Story #492: Determine scheduler leader for Server Status card
    # Query cluster_nodes for the node with role='scheduler' so it works
    # regardless of which node serves the request.
    scheduler_node_id = None
    try:
        _nm_backend = getattr(request.app.state, "node_metrics_backend", None)
        if _nm_backend is not None and hasattr(_nm_backend, "_pool"):
            with _nm_backend._pool.connection() as _sched_conn:
                with _sched_conn.cursor() as _sched_cur:
                    _sched_cur.execute(
                        "SELECT node_id FROM cluster_nodes "
                        "WHERE role = 'scheduler' AND status = 'online' "
                        "ORDER BY last_heartbeat DESC LIMIT 1"
                    )
                    _sched_row = _sched_cur.fetchone()
                    if _sched_row:
                        scheduler_node_id = _sched_row[0]
    except Exception as exc:
        logger.debug("Could not query scheduler from cluster_nodes: %s", exc)

    # Story #680: Compute dependency latency rows for the Server Status card.
    # Queries the last 5-minute window (current window only — aggregator operates
    # on current window; trend computation would need a separate previous window
    # query which is intentionally deferred until the full aggregation pipeline
    # is wired). Error resilience is intentional: dashboard display must never
    # crash the server even if the latency backend is unreachable.
    _CURRENT_WINDOW_S = 300.0  # 5 minutes — matches DependencyLatencyTracker retention
    dependency_rows = []
    try:
        _latency_tracker = getattr(request.app.state, "latency_tracker", None)
        if _latency_tracker is not None and _latency_tracker._backend is not None:
            import time as _time
            from code_indexer.server.services.dependency_latency_aggregator import (
                DependencyLatencyAggregator,
            )

            _now = _time.time()
            _samples = _latency_tracker._backend.select_samples_for_window(
                start_time=_now - _CURRENT_WINDOW_S,
                end_time=_now,
            )
            _storage_mode = getattr(request.app.state, "storage_mode", "sqlite")
            _clone_backend = getattr(request.app.state, "clone_backend", "none")
            _aggregator = DependencyLatencyAggregator()
            _stats = _aggregator.compute_stats(_samples)
            dependency_rows = _aggregator.build_rows(
                all_stats=_stats,
                storage_mode=_storage_mode,
                clone_backend=_clone_backend,
                registered_dep_names=[],
            )
    except (
        Exception
    ):  # broad catch intentional: dashboard display must never crash server
        logger.debug("Failed to compute dependency latency rows", exc_info=True)

    return templates.TemplateResponse(
        request,
        "partials/dashboard_health.html",
        {
            "request": request,
            "health": health_data,
            "database_health": database_health,
            "server_version": cidx_version,
            "node_metrics": node_metrics,
            "scheduler_node_id": scheduler_node_id,
            "dependency_rows": dependency_rows,
        },
    )


@web_router.get("/partials/dashboard-stats", response_class=HTMLResponse)
def dashboard_stats_partial(
    request: Request,
    time_filter: str = "24h",
    recent_filter: str = "24h",
    api_filter: int = 900,
):
    """
    Partial refresh endpoint for dashboard statistics section.

    Story #541 AC3/AC5: Support time filtering for job stats and recent activity.
    Rolling window API metrics: Support configurable time window for API activity.

    Args:
        request: HTTP request
        time_filter: Time filter for job stats ("24h", "7d", "30d")
        recent_filter: Time filter for recent activity ("24h", "7d", "30d")
        api_filter: Time window in seconds for API metrics (default 900).
            Common values: 60 (1 min), 900 (15 min), 3600 (1 hour), 86400 (24 hours)

    Returns HTML fragment for htmx partial updates.
    """
    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    # Story #503 AC1: Use backend_registry.api_metrics for cluster-wide aggregation.
    _backend_registry = getattr(request.app.state, "backend_registry", None)
    _api_metrics_backend = (
        _backend_registry.api_metrics if _backend_registry is not None else None
    )

    dashboard_service = _get_dashboard_service()
    # Story #712 AC6: Pass user_role to prevent activated repos count flash
    # Rolling window API metrics: Pass api_window for configurable time window
    stats_data = dashboard_service.get_stats_partial(
        session.username,
        time_filter=time_filter,
        recent_filter=recent_filter,
        user_role=session.role,
        api_window=api_filter,
        api_metrics_backend=_api_metrics_backend,
    )

    # AC1: Compute active deactivations count for the dashboard tile
    _job_manager = _get_background_job_manager()
    active_deactivations = (
        _job_manager.count_active_deactivations() if _job_manager else 0
    )

    return templates.TemplateResponse(
        request,
        "partials/dashboard_stats.html",
        {
            "request": request,
            "job_counts": stats_data["job_counts"],
            "repo_counts": stats_data["repo_counts"],
            "recent_jobs": stats_data["recent_jobs"],
            "api_metrics": stats_data.get("api_metrics", {}),
            "time_filter": time_filter,
            "recent_filter": recent_filter,
            "api_filter": api_filter,
            "active_deactivations": active_deactivations,
        },
    )


@web_router.get("/partials/dashboard-job-counts", response_class=HTMLResponse)
def dashboard_job_counts_partial(
    request: Request,
    time_filter: str = "24h",
):
    """
    Story #69: Granular partial endpoint for job counts data ONLY.

    Returns HTML fragment containing only job statistics cards,
    excluding dropdown controls to prevent disruption during auto-refresh.

    Args:
        request: HTTP request
        time_filter: Time filter for job stats ("24h", "7d", "30d")

    Returns HTML fragment for htmx partial updates.
    """
    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    dashboard_service = _get_dashboard_service()
    stats_data = dashboard_service.get_stats_partial(
        session.username,
        time_filter=time_filter,
        recent_filter="24h",  # Not used for job counts
        user_role=session.role,
        api_window=_DEFAULT_PER_USER_API_FILTER_SECONDS,  # Not used for job counts
    )

    return templates.TemplateResponse(
        request,
        "partials/dashboard_job_counts.html",
        {
            "request": request,
            "job_counts": stats_data["job_counts"],
            "time_filter": time_filter,
        },
    )


@web_router.get("/partials/dashboard-recent-jobs", response_class=HTMLResponse)
def dashboard_recent_jobs_partial(
    request: Request,
    recent_filter: str = "24h",
):
    """
    Story #69: Granular partial endpoint for recent jobs table body ONLY.

    Returns HTML fragment containing only table rows for recent jobs,
    excluding table structure and dropdown controls to prevent disruption during auto-refresh.

    Args:
        request: HTTP request
        recent_filter: Time filter for recent activity ("24h", "7d", "30d")

    Returns HTML fragment for htmx partial updates.
    """
    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    dashboard_service = _get_dashboard_service()
    stats_data = dashboard_service.get_stats_partial(
        session.username,
        time_filter="24h",  # Not used for recent jobs
        recent_filter=recent_filter,
        user_role=session.role,
        api_window=_DEFAULT_PER_USER_API_FILTER_SECONDS,  # Not used for recent jobs
    )

    return templates.TemplateResponse(
        request,
        "partials/dashboard_recent_jobs.html",
        {
            "request": request,
            "recent_jobs": stats_data["recent_jobs"],
            "recent_filter": recent_filter,
        },
    )


@web_router.get("/partials/dashboard-api-metrics", response_class=HTMLResponse)
def dashboard_api_metrics_partial(
    request: Request,
    api_filter: int = 900,
):
    """
    Story #69: Granular partial endpoint for API metrics data ONLY.

    Returns HTML fragment containing only API activity statistics cards,
    excluding dropdown controls to prevent disruption during auto-refresh.

    Args:
        request: HTTP request
        api_filter: Time window in seconds for API metrics (default 900).
            Common values: 60 (1 min), 900 (15 min), 3600 (1 hour), 86400 (24 hours)

    Returns HTML fragment for htmx partial updates.
    """
    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    # Story #503 AC1: Use backend_registry.api_metrics for cluster-wide aggregation.
    # In cluster (postgres) mode get_metrics_bucketed(node_id=None) aggregates all nodes.
    # In standalone mode the SQLite backend reads the local data — identical behaviour.
    backend_registry = getattr(request.app.state, "backend_registry", None)
    api_metrics_backend = (
        backend_registry.api_metrics if backend_registry is not None else None
    )

    # Story #503 AC2/AC3: Detect cluster mode to conditionally show per-node breakdown.
    is_cluster_mode = (
        api_metrics_backend is not None
        and "Postgres" in type(api_metrics_backend).__name__
    )

    dashboard_service = _get_dashboard_service()
    stats_data = dashboard_service.get_stats_partial(
        session.username,
        time_filter="24h",  # Not used for API metrics
        recent_filter="24h",  # Not used for API metrics
        user_role=session.role,
        api_window=api_filter,
        api_metrics_backend=api_metrics_backend,
    )

    # Story #503 AC2: Per-node breakdown — only in cluster mode.
    per_node_metrics: list[dict] = []
    if is_cluster_mode:
        try:
            _nm_backend = getattr(request.app.state, "node_metrics_backend", None)
            if _nm_backend is not None and api_metrics_backend is not None:
                node_rows = _nm_backend.get_latest_per_node()
                for row in node_rows:
                    node_id = row.get("node_id")
                    if node_id:
                        node_counts = api_metrics_backend.get_metrics_bucketed(
                            period_seconds=api_filter, node_id=node_id
                        )
                        per_node_metrics.append(
                            {"node_id": node_id, "metrics": node_counts}
                        )
        except Exception as _exc:
            logger.debug("Could not read per-node api_metrics: %s", _exc)

    return templates.TemplateResponse(
        request,
        "partials/dashboard_api_metrics.html",
        {
            "request": request,
            "api_metrics": stats_data.get("api_metrics", {}),
            "api_filter": api_filter,
            "is_cluster_mode": is_cluster_mode,
            "per_node_metrics": per_node_metrics,
        },
    )


@web_router.get("/partials/dashboard-cache-metrics", response_class=HTMLResponse)
def dashboard_cache_metrics_partial(request: Request, cache_window: int = 86400):
    """Story #1109 (S5) / Story #1294 / Bug #1391: Query-embedding cache
    metrics partial.

    Returns an HTML fragment showing total cached entries plus windowed,
    cluster-aggregated cache statistics for the selected time window. Every
    card EXCEPT Cache Entries (a live `query_embedding_cache` COUNT), Shadow
    Hit Rate, and On-Mode Hit Rate is sourced from WindowedCacheMetrics — a
    GROUP BY (cache_mode, provider) aggregation over the durable,
    phantom-free `search_embed_event` table (Story #1293), computed for
    `[now - cache_window, now)`. The two Hit Rate cards (Shadow and On-Mode)
    are BOTH row-denominated (each search_event_log row is one searched
    repository's cache lookup, Bug #2111) and windowed,
    sourced from `search_event_log` via `get_hit_rate_counts(mode, from_ts,
    to_ts)` (Issue #1257 introduced the request-denominated semantics for
    On-Mode; Bug #1391 fixed On-Mode to respect the selected time window and
    moved Shadow onto this same source/denominator for consistency — it was
    previously operation-denominated from search_embed_event and always
    unwindowed). This replaces the old in-memory
    QueryEmbeddingCacheMetrics/CoalescerRegistry per-node, volatile tallies
    (Story #1294) — the numbers are now durable, windowed, and aggregated
    across every cluster node by construction (the backend reads the shared
    store, never a per-node counter).

    Args:
        request: HTTP request.
        cache_window: Time window in seconds for the windowed cache metrics
            (default 86400 == 24h), mirroring the api_filter convention.

    Returns HTML fragment for htmx partial updates.
    """
    import types

    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    # total_entries: reuse the same helper used by the config-page readout so the
    # dashboard and the config page always show the same live count. Cache
    # Entries is current cache STATE, not an event — it is NEVER windowed.
    total_entries = _get_qec_total_entries()

    # Story #1294: WindowedCacheMetrics — durable, windowed, cluster-aggregated.
    # Fail-open: any error (writer absent, backend raises) falls back to
    # empty_windowed_result() so the dashboard never breaks (AC5).
    from code_indexer.server.services.windowed_cache_metrics import (
        empty_windowed_result,
    )

    to_ts = time.time()
    from_ts = to_ts - cache_window
    see_writer = getattr(request.app.state, "search_embed_event_writer", None)
    see_backend = see_writer.backend if see_writer is not None else None
    if see_backend is not None:
        try:
            windowed = see_backend.get_windowed_metrics(from_ts, to_ts)
        except Exception as _exc:
            logger.warning(
                "dashboard_cache_metrics_partial: get_windowed_metrics failed: %s",
                _exc,
            )
            windowed = empty_windowed_result()
    else:
        windowed = empty_windowed_result()

    overall = windowed.overall
    shadow_agg = windowed.by_cache_mode.get("shadow")
    if shadow_agg is None:
        from code_indexer.server.services.windowed_cache_metrics import (
            CacheMetricsAggregate,
        )

        shadow_agg = CacheMetricsAggregate()

    # Bug #1391 Defect 2: Shadow Hit Rate is no longer sourced from this
    # search_embed_event shadow_agg (operation-denominated -- one row per
    # NEEDED embed per provider). It is computed below from search_event_log
    # instead, alongside On-Mode, so both Hit Rate cards share the SAME
    # request-denominated source/window. The cosine distribution fields
    # below correctly REMAIN sourced from shadow_agg (per-sample
    # distributions are operation-based by design).
    shadow_hits = 0
    shadow_requests = 0
    shadow_cosine_p50 = shadow_agg.shadow_cosine_p50
    shadow_cosine_p05 = shadow_agg.shadow_cosine_p05
    shadow_cosine_min = shadow_agg.shadow_cosine_min
    shadow_cosine_histogram = shadow_agg.shadow_cosine_histogram

    long_key = overall.long_key
    audit_total = overall.audit_count
    audit_overlap_avg = overall.audit_avg if overall.audit_count > 0 else None

    coalescer_texts_coalesced = overall.texts_coalesced
    coalescer_batches_dispatched = overall.batches
    coalescer_dedup_savings = overall.dedup
    coalescer_provider_embed_calls = overall.provider_embed_calls

    # Issue #1257 / Bug #1391: Both Hit Rate cards are REQUEST-denominated
    # (each search_event_log row is one searched repository's cache
    # lookup), NOT
    # operation-denominated, and BOTH now respect the selected cache_window
    # (from_ts/to_ts) instead of one of them (On-Mode) aggregating the
    # table's entire lifetime. Each call fails open independently: a failure
    # on one mode's lookup must not suppress the other card.
    on_hits = 0
    on_requests = 0
    sel_writer = getattr(request.app.state, "search_event_log_writer", None)
    sel_backend = sel_writer.backend if sel_writer is not None else None
    if sel_backend is not None:
        try:
            _on_counts = sel_backend.get_hit_rate_counts("on", from_ts, to_ts)
            on_hits = _on_counts.get("hits", 0)
            on_requests = _on_counts.get("requests", 0)
        except Exception as _exc:
            logger.warning(
                "dashboard_cache_metrics_partial: get_hit_rate_counts('on') failed: %s",
                _exc,
            )
        try:
            _shadow_counts = sel_backend.get_hit_rate_counts("shadow", from_ts, to_ts)
            shadow_hits = _shadow_counts.get("hits", 0)
            shadow_requests = _shadow_counts.get("requests", 0)
        except Exception as _exc:
            logger.warning(
                "dashboard_cache_metrics_partial: get_hit_rate_counts('shadow') "
                "failed: %s",
                _exc,
            )

    cache_metrics = types.SimpleNamespace(
        total_entries=total_entries,
        shadow_hits=shadow_hits,
        shadow_requests=shadow_requests,
        on_hits=on_hits,
        on_requests=on_requests,
        shadow_cosine_p50=shadow_cosine_p50,
        shadow_cosine_histogram=shadow_cosine_histogram,
        shadow_cosine_min=shadow_cosine_min,
        shadow_cosine_p05=shadow_cosine_p05,
        audit_total=audit_total,
        audit_overlap_avg=audit_overlap_avg,
        long_key=long_key,
        # Story #1146 dedup counters — now windowed (Story #1294), not
        # per-node in-memory coalescer tallies.
        coalescer_texts_coalesced=coalescer_texts_coalesced,
        coalescer_batches_dispatched=coalescer_batches_dispatched,
        coalescer_dedup_savings=coalescer_dedup_savings,
        coalescer_provider_embed_calls=coalescer_provider_embed_calls,
    )

    # Resolve the current node identifier — retained for any node-scoped UX
    # elsewhere on the page; the cache-metrics cards themselves are no longer
    # per-node (Story #1294 made them cluster-aggregated).
    node_id: str = getattr(request.app.state, "node_id", None) or socket.gethostname()

    # Story #1152: precompute log10-scaled bar percentages in Python so the
    # template never needs the nonexistent Jinja `log` filter (which causes a
    # 500 on any populated histogram bucket).
    # histogram_bars is a list of dicts: {lo, hi, count, bar_pct (0-100 int)}.
    # bar_pct is log10(count+1) normalised to the max bucket's log10 value.
    # Empty histogram or all-zero counts -> all bar_pct=0 (no division).
    import math

    histogram_bars: List[Dict[str, Any]] = []
    if shadow_cosine_histogram:
        bars_log = [math.log10(c + 1) for (_, _, c) in shadow_cosine_histogram]
        max_bar = max(bars_log) if bars_log else 0.0
        for (lo, hi, count), blog in zip(shadow_cosine_histogram, bars_log):
            bar_pct = round(100 * blog / max_bar) if max_bar > 0 else 0
            histogram_bars.append(
                {"lo": lo, "hi": hi, "count": count, "bar_pct": bar_pct}
            )

    return templates.TemplateResponse(
        request,
        "partials/dashboard_cache_metrics.html",
        {
            "request": request,
            "cache_metrics": cache_metrics,
            "node_id": node_id,
            "histogram_bars": histogram_bars,
            "cache_window": cache_window,
        },
    )


@web_router.get("/partials/dashboard-langfuse", response_class=HTMLResponse)
def dashboard_langfuse_partial(request: Request):
    """
    Story #168: Langfuse status card partial endpoint.

    Returns HTML fragment containing Langfuse sync status, metrics, and manual sync trigger.
    Card is only visible when langfuse.pull_enabled is true.

    Auto-refreshes every 30 seconds via HTMX.

    Returns HTML fragment for htmx partial updates.
    """
    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    dashboard_service = _get_dashboard_service()
    langfuse_data = dashboard_service.get_langfuse_metrics()

    return templates.TemplateResponse(
        request,
        "partials/dashboard_langfuse.html",
        {
            "request": request,
            "langfuse": langfuse_data,
        },
    )


@web_router.get("/admin/cache-sample")
def admin_cache_sample(
    request: Request,
    limit: int = Query(
        20,
        ge=1,
        le=200,
        description="Number of recent cache rows to return (default 20, max 200).",
    ),
):
    """Story #1149: Admin cache-sample readout.

    Returns the most-recently-used rows from the query-embedding cache as
    metadata-only tuples: (cache_key, provider, model, dimension, key_length).
    NEVER returns embedding vectors or any secret material.

    This endpoint enables cluster E2E verification of the key shape (including
    the 's:<config-digest>:' prefix) without requiring direct database access.

    Requires admin session.  Returns JSON.

    Args:
        request: HTTP request.
        limit: Number of recent rows to return (1-200, default 20).

    Returns:
        JSON: {"rows": [...], "count": int}  where each row is a dict with
        keys: cache_key, provider, model, dimension, key_length.
    """
    from fastapi.responses import JSONResponse

    session = _require_admin_session(request)
    if not session:
        return JSONResponse(content={"error": "Unauthorized"}, status_code=401)

    rows = []
    try:
        from code_indexer.server.services.governed_call import get_query_embedding_cache

        cache = get_query_embedding_cache()
        if cache is not None:
            backend = cache._backend  # type: ignore[attr-defined]
            if hasattr(backend, "select_recent"):
                rows = backend.select_recent(limit=limit)
    except Exception as exc:
        logger.warning("admin_cache_sample: failed to fetch rows (fail-open): %s", exc)

    return JSONResponse(content={"rows": rows, "count": len(rows)})


@web_router.get("/partials/dashboard-api-per-user", response_class=HTMLResponse)
def dashboard_api_per_user_partial(
    request: Request,
    api_filter: int = Query(
        _DEFAULT_PER_USER_API_FILTER_SECONDS,
        ge=60,
        le=31_536_000,
        description="Time window in seconds for per-user API usage breakdown.",
    ),
):
    """
    Story #674: Per-user API usage breakdown card partial endpoint.

    Returns HTML fragment containing a table of API usage counts per user,
    sorted by total descending, for the selected time window.

    Args:
        request: HTTP request
        api_filter: Time window in seconds (60s min, 1 year max; default 24h).

    Returns HTML fragment for htmx partial updates.
    """
    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    backend_registry = getattr(request.app.state, "backend_registry", None)
    api_metrics_backend = (
        backend_registry.api_metrics if backend_registry is not None else None
    )

    dashboard_service = _get_dashboard_service()
    per_user_data = dashboard_service.get_per_user_stats(
        period_seconds=api_filter,
        api_metrics_backend=api_metrics_backend,
    )

    return templates.TemplateResponse(
        request,
        "partials/dashboard_api_per_user.html",
        {
            "request": request,
            "per_user_data": per_user_data,
            "api_filter": api_filter,
        },
    )


@web_router.get("/partials/dashboard-api-chart", response_class=HTMLResponse)
def dashboard_api_chart_partial(
    request: Request,
    api_filter: int = Query(
        _DEFAULT_PER_USER_API_FILTER_SECONDS,
        ge=60,
        le=31_536_000,
        description="Time window in seconds for API usage chart.",
    ),
):
    """
    Story #675: API Usage Trends stacked bar chart partial endpoint.

    Returns HTML fragment containing a canvas element and inline Chart.js
    initialization script that renders a stacked bar chart of API usage over time.

    Args:
        request: HTTP request
        api_filter: Time window in seconds (60s min, 1 year max; default 24h).

    Returns HTML fragment for htmx partial updates.
    """
    session = _require_admin_session(request)
    if not session:
        return HTMLResponse(content="", status_code=401)

    backend_registry = getattr(request.app.state, "backend_registry", None)
    api_metrics_backend = (
        backend_registry.api_metrics if backend_registry is not None else None
    )

    dashboard_service = _get_dashboard_service()
    chart_data = dashboard_service.get_chart_data(
        period_seconds=api_filter,
        api_metrics_backend=api_metrics_backend,
    )

    return templates.TemplateResponse(
        request,
        "partials/dashboard_api_chart.html",
        {
            "request": request,
            "chart_data": chart_data,
            "api_filter": api_filter,
        },
    )


@web_router.post(
    "/langfuse-sync/trigger",
    response_class=JSONResponse,
    dependencies=[Depends(dependencies.require_elevation())],
)
def langfuse_sync_trigger(request: Request):
    """
    Story #168 AC4: Trigger immediate Langfuse sync.

    C1 fix: Non-blocking trigger using background thread, returns 202 Accepted.
    H4 fix: Returns 409 Conflict if sync already in progress.

    Returns:
        202 Accepted - Sync triggered successfully
        409 Conflict - Sync already in progress
        503 Service Unavailable - Sync service not initialized
        401 Unauthorized - Authentication required
    """
    session = _require_admin_session(request)
    if not session:
        return JSONResponse(
            {"status": "error", "message": "Authentication required"},
            status_code=status.HTTP_401_UNAUTHORIZED,
        )

    try:
        from ...app import app as _app

        langfuse_sync_service = getattr(
            cast(Any, _app).state, "langfuse_sync_service", None
        )

        if langfuse_sync_service is None:
            return JSONResponse(
                {"status": "error", "message": "Langfuse sync service not initialized"},
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            )

        # C1/H4: Non-blocking trigger with concurrent sync guard
        triggered = langfuse_sync_service.trigger_sync()

        if not triggered:
            return JSONResponse(
                {"status": "error", "message": "Sync already in progress"},
                status_code=status.HTTP_409_CONFLICT,
            )

        return JSONResponse(
            {"status": "success", "message": "Sync triggered successfully"},
            status_code=status.HTTP_202_ACCEPTED,
        )

    except Exception as e:
        logger.error(
            format_error_log(
                "LANGFUSE-SYNC-001",
                f"Failed to trigger Langfuse sync: {e}",
            ),
            exc_info=True,
        )
        # M5: Generic error message (keep detailed logging above)
        return JSONResponse(
            {"status": "error", "message": "Internal error triggering sync"},
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        )
