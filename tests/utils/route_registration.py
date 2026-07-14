"""
Helpers for asserting that a route is registered on a FastAPI app.

Why this exists
---------------
Scanning ``app.routes`` directly and reading ``route.path`` USED to see every
route, because ``include_router()`` copied the sub-router's routes into the
app's own route list. As of FastAPI 0.13x (``fastapi>=0.116.0`` resolves to it,
and it is what the server actually runs) ``include_router()`` instead appends a
single ``fastapi.routing._IncludedRouter`` object per inclusion, which holds the
sub-router in ``.original_router`` and its mount prefix in ``.include_context``.

``_IncludedRouter`` has NO ``.path``, so the old pattern breaks in two ways:

1. ``[r.path for r in app.routes]`` raises ``AttributeError``.
2. ``[getattr(r, "path", None) for r in app.routes]`` does NOT raise -- it just
   silently misses every included route. On this app that is the difference
   between seeing **80** paths and the real **395**: a route-registration
   assertion written that way can pass while asserting almost nothing.

The routes are still registered and served -- only this introspection changed.
These helpers walk into ``_IncludedRouter`` so the assertions see the whole app
again, and they stay correct on older FastAPI (where there is nothing to walk
into) because they simply read ``.path`` when it is present.
"""

from __future__ import annotations

from typing import Any, Iterable, List, Optional, Set


def iter_routes(routes: Iterable[Any]) -> List[Any]:
    """Flatten a FastAPI/Starlette route list, descending into included routers.

    Args:
        routes: ``app.routes`` (or any nested router's ``.routes``).

    Returns:
        Every leaf route object (those carrying a ``.path``), with included
        routers expanded. Leaf objects are returned as-is so callers can still
        inspect ``.methods``, ``.name``, etc.
    """
    flat: List[Any] = []
    for route in routes:
        if getattr(route, "path", None) is not None:
            flat.append(route)
            continue

        # FastAPI >= 0.13x: include_router() appends an _IncludedRouter holding
        # the original APIRouter plus the prefix it was mounted under.
        original = getattr(route, "original_router", None)
        if original is not None:
            flat.extend(iter_routes(getattr(original, "routes", [])))
    return flat


def route_paths(app_or_router: Any) -> Set[str]:
    """Return every registered path on an app/router, including included routers.

    Prefixes from ``include_router(prefix=...)`` are applied, so the paths match
    what a client would actually call.
    """
    return {
        prefix + route.path
        for prefix, route in _iter_prefixed(getattr(app_or_router, "routes", []))
    }


def find_route(app_or_router: Any, path: str) -> Optional[Any]:
    """Return the leaf route registered at ``path``, or None.

    Use when the assertion needs more than existence -- e.g. checking
    ``route.methods`` to reject a misconfigured GET-only endpoint.
    """
    for prefix, route in _iter_prefixed(getattr(app_or_router, "routes", [])):
        if prefix + route.path == path:
            return route
    return None


def _iter_prefixed(routes: Iterable[Any], prefix: str = "") -> List[Any]:
    """Yield (accumulated_prefix, leaf_route) pairs."""
    out: List[Any] = []
    for route in routes:
        if getattr(route, "path", None) is not None:
            out.append((prefix, route))
            continue

        original = getattr(route, "original_router", None)
        if original is None:
            continue

        context = getattr(route, "include_context", None)
        sub_prefix = getattr(context, "prefix", "") or ""
        out.extend(_iter_prefixed(getattr(original, "routes", []), prefix + sub_prefix))
    return out
