"""The routes an app or router serves, at their full paths.

FastAPI before 0.16x copied an included router's routes into the including
router, so ``router.routes`` was the flat table. From 0.16x
``include_router`` keeps the included router nested (an ``_IncludedRouter``
holding ``original_router`` and an ``include_context`` with the include's
prefix), and ``router.routes`` lists those wrappers instead: a walk over it
alone sees none of the routes they hold. Anything that inspects a route
table (the feature loader's collision check, route lookups in tests) walks
it through :func:`iter_routes`, which works with both.

Under 0.16x a route's own ``dependencies`` / ``dependant`` hold only what
the route and its router declare: dependencies given to ``include_router``
stay on the include (only the feature loader adds any, for feature
routers). Code that needs those reads them from the app, not the route.
"""

from __future__ import annotations

from typing import Any, Iterable, Iterator, Tuple

from starlette.routing import BaseRoute


def iter_routes(
    routes: Iterable[Any], prefix: str = ""
) -> Iterator[Tuple[str, BaseRoute]]:
    """``(full path, route)`` for every route in *routes*, descending into
    included routers; *prefix* is prepended to every path."""
    for route in routes:
        included = getattr(route, "original_router", None)
        if included is not None:
            context = getattr(route, "include_context", None)
            yield from iter_routes(
                included.routes, prefix + (getattr(context, "prefix", "") or "")
            )
        else:
            yield prefix + (getattr(route, "path", "") or ""), route
