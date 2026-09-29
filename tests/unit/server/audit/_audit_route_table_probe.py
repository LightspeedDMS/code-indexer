"""Print the real server's route table as JSON (run as a separate process).

Used by ``test_catalog_completeness``.  The routes are registered by the
production wiring (``create_fastapi_app``) with a real ``ConfigService`` on
the server directory named by ``CIDX_SERVER_DATA_DIR`` and no managers
(route registration only stores references), plus EVERY router the lifespan
mounts at startup: each ``include_router`` call in ``startup/lifespan.py`` is
found in that file's syntax tree and its router resolved through the file's
own imports, so a router the lifespan starts mounting is inventoried without
editing this probe.  The fault-injection admin router, which
``wire_fault_injection`` mounts only when that non-production harness is
enabled, is mounted too.  Running in its own process keeps the wiring's
module-level side effects (dependency singletons, the wiki cache) out of the
test process.

Every route kind is accounted for: FastAPI and Starlette routes are listed,
a mount must be static files, and anything else stops the probe.

Usage: ``python _audit_route_table_probe.py OUTPUT_JSON``.

Output: one JSON list; each entry names the route (``"METHOD /path"``), the
qualified names of every dependency in its dependency tree, the permission
of any ``require_permission`` dependency, and the endpoint's source file and
source text.
"""

from __future__ import annotations

import ast
import contextlib
import importlib
import importlib.util
import inspect
import json
import os
import sys
import textwrap
from pathlib import Path
from typing import Any, AsyncIterator, Dict, List, Set, Tuple

_LIFESPAN_PACKAGE = "code_indexer.server.startup"


def _dependency_names(dependant: Any, names: Set[str], perms: Set[str]) -> None:
    for dep in dependant.dependencies:
        call = dep.call
        qualname = getattr(call, "__qualname__", type(call).__name__)
        names.add(qualname)
        if qualname.startswith("require_permission."):
            for cell in call.__closure__ or ():
                if isinstance(cell.cell_contents, str):
                    perms.add(cell.cell_contents)
        _dependency_names(dep, names, perms)


def _entry(method: str, route: Any, names: Set[str], perms: Set[str]) -> Dict:
    endpoint = inspect.unwrap(route.endpoint)
    return {
        "key": f"{method} {route.path}",
        "dependencies": sorted(names),
        "permissions": sorted(perms),
        "file": inspect.getsourcefile(endpoint),
        "line": inspect.getsourcelines(endpoint)[1],
        "source": textwrap.dedent(inspect.getsource(endpoint)),
    }


def _route_entries(app: Any) -> List[Dict[str, Any]]:
    from fastapi.routing import APIRoute
    from starlette.routing import Mount, Route
    from starlette.staticfiles import StaticFiles

    entries: List[Dict[str, Any]] = []
    for route in app.routes:
        names: Set[str] = set()
        perms: Set[str] = set()
        if isinstance(route, APIRoute):
            _dependency_names(route.dependant, names, perms)
        elif isinstance(route, Mount):
            if not isinstance(route.app, StaticFiles):
                raise SystemExit(f"unclassifiable mount at {route.path}")
            continue  # static files: read-only, no endpoint of ours
        elif not isinstance(route, Route):
            raise SystemExit(f"unknown route kind {type(route).__name__}")
        for method in sorted(route.methods or ()):
            entries.append(_entry(method, route, names, perms))
    return entries


def _import_target(node: ast.ImportFrom, alias: ast.alias) -> Tuple[str, str]:
    module = importlib.util.resolve_name(
        "." * node.level + (node.module or ""), _LIFESPAN_PACKAGE
    )
    return module, alias.name


def _resolve(module: str, name: str) -> Any:
    try:
        return importlib.import_module(f"{module}.{name}")
    except ModuleNotFoundError:
        return getattr(importlib.import_module(module), name)


def _lifespan_routers() -> List[Any]:
    """Every router ``startup/lifespan.py`` passes to ``include_router``."""
    lifespan = importlib.import_module(f"{_LIFESPAN_PACKAGE}.lifespan")
    tree = ast.parse(Path(str(lifespan.__file__)).read_text(encoding="utf-8"))
    bound: Dict[str, Tuple[str, str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                bound[alias.asname or alias.name] = _import_target(node, alias)
    routers: List[Any] = []
    for node in ast.walk(tree):
        if not (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "include_router"
        ):
            continue
        arg = node.args[0]
        if isinstance(arg, ast.Attribute) and isinstance(arg.value, ast.Name):
            base, attribute = arg.value.id, arg.attr
        elif isinstance(arg, ast.Name):
            base, attribute = arg.id, ""
        else:
            raise SystemExit(f"unresolvable include_router at lifespan:{node.lineno}")
        if base not in bound:
            raise SystemExit(f"include_router of an unimported name: {base}")
        target = _resolve(*bound[base])
        routers.append(getattr(target, attribute) if attribute else target)
    if not routers:
        raise SystemExit("the lifespan mounts no router: the scan is stale")
    return routers


def main() -> int:
    from code_indexer.server.services.config_service import ConfigService
    from code_indexer.server.startup.app_wiring import create_fastapi_app

    server_dir = os.environ["CIDX_SERVER_DATA_DIR"]
    config_service = ConfigService(server_dir_path=server_dir)
    config_service.load_config()

    class _NoManagers(dict):
        def __missing__(self, key: str) -> None:
            return None

    services = _NoManagers(
        server_config=config_service.get_config(),
        config_service=config_service,
        data_dir=server_dir,
        db_path_str=os.path.join(server_dir, "data", "cidx_server.db"),
        secret_key="route-table-probe-signing-key-000000",
    )

    @contextlib.asynccontextmanager
    async def _no_lifespan(_app: Any) -> AsyncIterator[None]:
        yield

    app = create_fastapi_app(services, _no_lifespan)
    # The routers the lifespan mounts at startup, as its own source names them.
    for router in _lifespan_routers():
        app.include_router(router)
    # Mounted by wire_fault_injection only when the non-production harness is
    # enabled; inventoried unconditionally so its doors are always classified.
    from code_indexer.server.fault_injection.router import router as fault_router

    app.include_router(fault_router)
    with open(sys.argv[1], "w", encoding="utf-8") as out:
        json.dump(_route_entries(app), out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
