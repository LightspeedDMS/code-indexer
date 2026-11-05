"""The web UI route table is pinned while ``web/routes.py`` is split (modular phase 6).

``web/routes.py`` used to define every admin, user, login and public web route in
one module. Its routes now live in per-domain modules under ``web/pages/`` and
``web/routes.py`` aggregates them into the four routers the app mounts
(``web_router`` at ``/admin``, ``user_router`` at ``/user``, ``login_router`` at
the root and ``api_router`` at ``/api``).

``web_route_table_snapshot.json`` was recorded from the single-module
``web/routes.py`` before the split. This test compares the live routers with it:

* every route keeps its methods, path, url_for name, endpoint function name,
  response class, status code, schema visibility, request parameters and
  dependency set, on the same router;
* no route is added or removed;
* any two routes on one router that can match the same request keep their
  relative order (Starlette serves the first match, so only the order of
  overlapping routes is observable).

To regenerate the snapshot after an intended route change:
``python3 tests/unit/server/web/test_web_route_table_snapshot.py --write``.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List

from fastapi.datastructures import DefaultPlaceholder
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute
from starlette.routing import BaseRoute
from code_indexer.server.utils.route_table import iter_routes

SNAPSHOT_PATH = Path(__file__).with_name("web_route_table_snapshot.json")

# Modules whose functions moved during the split; a dependency defined in one of
# them is recorded by name only so the move itself is not a difference.
_MOVED_MODULE_PREFIXES = (
    "code_indexer.server.web.routes",
    "code_indexer.server.web.pages",
)


def _callable_id(call: Any) -> str:
    module = getattr(call, "__module__", "") or ""
    name = getattr(call, "__qualname__", None) or type(call).__qualname__
    if module.startswith(_MOVED_MODULE_PREFIXES):
        return f"<web>.{name}"
    return f"{module}.{name}"


def _dependency_ids(dependant: Dependant) -> List[str]:
    found: List[str] = []
    for dep in dependant.dependencies:
        if dep.call is not None:
            found.append(_callable_id(dep.call))
        found.extend(_dependency_ids(dep))
    return sorted(found)


def _param_names(dependant: Dependant) -> Dict[str, List[str]]:
    return {
        "path": sorted(p.name for p in dependant.path_params),
        "query": sorted(p.name for p in dependant.query_params),
        "header": sorted(p.name for p in dependant.header_params),
        "cookie": sorted(p.name for p in dependant.cookie_params),
        "body": sorted(p.name for p in dependant.body_params),
    }


def _route_entry(route: BaseRoute) -> Dict[str, Any]:
    if not isinstance(route, APIRoute):
        return {"kind": type(route).__name__, "path": getattr(route, "path", "")}
    response_class: Any = route.response_class
    if isinstance(response_class, DefaultPlaceholder):
        response_class = f"default:{response_class.value.__name__}"
    else:
        response_class = response_class.__name__
    return {
        "kind": "APIRoute",
        "path": route.path,
        "methods": sorted(route.methods or ()),
        "name": route.name,
        "endpoint": route.endpoint.__name__,
        "response_class": response_class,
        "status_code": route.status_code,
        "include_in_schema": route.include_in_schema,
        "tags": [str(t) for t in route.tags],
        "params": _param_names(route.dependant),
        "dependencies": _dependency_ids(route.dependant),
    }


def build_route_table() -> Dict[str, List[Dict[str, Any]]]:
    """Return the route table of the four routers ``web/routes.py`` exports."""
    from code_indexer.server.web import routes

    return {
        "web_router": [
            _route_entry(r) for route_path, r in iter_routes(routes.web_router.routes)
        ],
        "user_router": [
            _route_entry(r) for route_path, r in iter_routes(routes.user_router.routes)
        ],
        "login_router": [
            _route_entry(r) for route_path, r in iter_routes(routes.login_router.routes)
        ],
        "api_router": [
            _route_entry(r) for route_path, r in iter_routes(routes.api_router.routes)
        ],
    }


def _load_snapshot() -> Dict[str, List[Dict[str, Any]]]:
    loaded: Dict[str, List[Dict[str, Any]]] = json.loads(
        SNAPSHOT_PATH.read_text(encoding="utf-8")
    )
    return loaded


def _key(entry: Dict[str, Any]) -> str:
    return json.dumps(entry, sort_keys=True)


def _segments_overlap(a: str, b: str) -> bool:
    """True when some request path can match both route templates.

    A ``{param}`` segment matches any single segment; a ``{param:path}`` segment
    matches one or more segments (Starlette's ``path`` convertor).
    """

    def split(path: str) -> List[str]:
        return [s for s in path.strip("/").split("/")]

    def is_param(seg: str) -> bool:
        return seg.startswith("{") and seg.endswith("}")

    def is_path_param(seg: str) -> bool:
        return is_param(seg) and seg.endswith(":path}")

    def match(xs: List[str], ys: List[str]) -> bool:
        if not xs or not ys:
            return not xs and not ys
        x, y = xs[0], ys[0]
        if is_path_param(x):
            return any(match(xs[1:], ys[i:]) for i in range(1, len(ys) + 1))
        if is_path_param(y):
            return any(match(xs[i:], ys[1:]) for i in range(1, len(xs) + 1))
        if is_param(x) or is_param(y) or x == y:
            return match(xs[1:], ys[1:])
        return False

    return match(split(a), split(b))


def _routes_conflict(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
    if a.get("kind") != "APIRoute" or b.get("kind") != "APIRoute":
        return _segments_overlap(a["path"], b["path"])
    if not set(a["methods"]) & set(b["methods"]):
        return False
    return _segments_overlap(a["path"], b["path"])


def test_route_table_entries_unchanged() -> None:
    expected = _load_snapshot()
    actual = build_route_table()
    assert sorted(actual) == sorted(expected)
    for router_name, expected_routes in expected.items():
        expected_keys = sorted(_key(e) for e in expected_routes)
        actual_keys = sorted(_key(e) for e in actual[router_name])
        missing = sorted(set(expected_keys) - set(actual_keys))
        added = sorted(set(actual_keys) - set(expected_keys))
        assert not missing and not added, (
            f"{router_name}: routes differ from the snapshot\n"
            f"missing: {missing}\nadded: {added}"
        )
        assert actual_keys == expected_keys, f"{router_name}: duplicate count changed"


def test_overlapping_routes_keep_their_relative_order() -> None:
    expected = _load_snapshot()
    actual = build_route_table()
    for router_name, expected_routes in expected.items():
        position = {_key(e): i for i, e in enumerate(actual[router_name])}
        for i, first in enumerate(expected_routes):
            for second in expected_routes[i + 1 :]:
                if not _routes_conflict(first, second):
                    continue
                assert position[_key(first)] < position[_key(second)], (
                    f"{router_name}: {first['path']} ({first.get('name')}) must "
                    f"still be matched before {second['path']} ({second.get('name')})"
                )


def test_overlap_detection_examples() -> None:
    assert _segments_overlap("/config/{section}", "/config/reset")
    assert not _segments_overlap("/config/{section}", "/config/a/b")
    assert _segments_overlap(
        "/groups/users/{user_id:path}/assign", "/groups/users/a/b/assign"
    )
    assert not _segments_overlap("/users", "/groups")
    assert _segments_overlap("/", "/")


if __name__ == "__main__" and sys.argv[1:] == ["--write"]:
    SNAPSHOT_PATH.write_text(
        json.dumps(build_route_table(), indent=1, sort_keys=True) + "\n",
        encoding="utf-8",
    )
