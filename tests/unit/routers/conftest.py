"""Shared pytest fixtures and helpers for tests/unit/routers/.

Bug #1807 root cause: FastAPI's `app.dependency_overrides` matches by the
EXACT callable object a route declares (via `Depends(...)`, whether that
`Depends` is a function-parameter default or a route-decorator-level
`dependencies=[...]` extra). An override registered against a callable no
route actually depends on is a silent no-op -- real auth runs instead, and
every request "protected" by that dead override gets whatever status the
real dependency happens to produce. In Bug #1807's case that was an
unconditional 401 from every write endpoint on the repo-categories router
(the route had moved to the hybrid-auth dependency chain,
`require_elevation()` -> `get_current_admin_user_hybrid`, while the test
file's overrides still targeted the pre-refactor `get_current_admin_user`),
degrading 14 of 17 tests in test_repo_categories_api.py to false negatives
for months with zero signal at override-registration time.

`assert_dependency_is_wired()` / the `set_dependency_override` fixture close
that gap: they walk the FULL dependency graph of every route registered on
`app` (recursively, so nested dependencies like `require_elevation()`'s own
`Depends(get_current_admin_user_hybrid)` are included) and fail loudly, at
override-installation time, if the target callable is not reachable from any
route. A future auth refactor that changes which callable a router declares
will now fail here -- with a clear assertion message naming the dead
override -- instead of silently degrading every test behind it to a uniform
401/403.
"""

from typing import Callable, Set

import pytest
from fastapi import FastAPI
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute, APIWebSocketRoute

# Both HTTP and WebSocket routes expose a `.dependant` graph (built by
# FastAPI's `get_dependant()`); traversal must cover both or a valid
# override used only by a WebSocket route would be misreported as dead.
_ROUTE_TYPES_WITH_DEPENDANT = (APIRoute, APIWebSocketRoute)


def collect_all_dependency_callables(app: FastAPI) -> Set[Callable]:
    """Collect every dependency callable declared by any route on `app`.

    Includes both function-parameter `Depends(...)` and route-decorator-level
    `dependencies=[Depends(...)]` extras -- FastAPI folds both into
    `route.dependant.dependencies` at route-construction time (see
    `fastapi.routing.APIRoute.__init__`). Traversal deliberately starts from
    each route's `dependant.dependencies` (its immediate `Depends(...)`
    children), NOT from `dependant.call` itself -- that top-level `.call` is
    the route's own endpoint function, which `dependency_overrides` never
    applies to, so including it would let a dead override targeting an
    endpoint function falsely pass this check. The inner `_walk` closure
    recurses each child sub-tree (adding every nested dependant's `.call`,
    e.g. `require_elevation()` depending on `get_current_admin_user_hybrid`)
    and stays local because it only ever mutates `callables`, the set it
    closes over.
    """
    callables: Set[Callable] = set()

    def _walk(dependant: Dependant) -> None:
        if dependant.call is not None:
            callables.add(dependant.call)
        for sub_dependant in dependant.dependencies:
            _walk(sub_dependant)

    for route in app.routes:
        if isinstance(route, _ROUTE_TYPES_WITH_DEPENDANT):
            for sub_dependant in route.dependant.dependencies:
                _walk(sub_dependant)
    return callables


def assert_dependency_is_wired(app: FastAPI, dependency: Callable) -> None:
    """Fail loudly if no route on `app` actually depends on `dependency`.

    Call this immediately before/while installing a `dependency_overrides`
    entry so a stale override is caught at test setup time -- not discovered
    months later as a wall of unrelated-looking 401/403 assertion failures
    (Bug #1807).
    """
    all_callables = collect_all_dependency_callables(app)
    assert dependency in all_callables, (
        f"{dependency!r} is not declared as a dependency by ANY route on "
        f"this app. Overriding it via app.dependency_overrides is a SILENT "
        f"NO-OP -- FastAPI only consults dependency_overrides for callables "
        f"a route actually depends on, so real auth would run instead and "
        f"most likely reject the (unauthenticated TestClient) request "
        f"(Bug #1807). Check which auth dependency the route under test "
        f"actually declares -- including route-decorator-level "
        f"`dependencies=[...]` extras such as require_elevation() -- and "
        f"override that callable instead."
    )


@pytest.fixture
def set_dependency_override() -> Callable[[FastAPI, Callable, Callable], None]:
    """Return a helper that installs a dependency override, asserting it is wired.

    Usage: `set_dependency_override(app, real_dependency_callable, mock_fn)`.
    Guards against Bug #1807: a stale override (targeting a callable no
    route depends on) raises AssertionError immediately instead of silently
    doing nothing.
    """

    def _set(app: FastAPI, dependency: Callable, override: Callable) -> None:
        assert_dependency_is_wired(app, dependency)
        app.dependency_overrides[dependency] = override

    return _set
