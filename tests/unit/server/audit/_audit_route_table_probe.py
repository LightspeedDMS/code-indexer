"""Print the real server's route table as JSON (run as a separate process).

Used by ``test_catalog_completeness``.  The routes are registered by the
production wiring (``create_fastapi_app``) with a real ``ConfigService`` on
the server directory named by ``CIDX_SERVER_DATA_DIR`` and no managers
(route registration only stores references), plus the SSO router that the
lifespan always mounts at startup.  Running in its own process
keeps the wiring's module-level side effects (dependency singletons, the
wiki cache) out of the test process.

Usage: ``python _audit_route_table_probe.py OUTPUT_JSON``.

Output: one JSON list; each entry names the route (``"METHOD /path"``), the
qualified names of every dependency in its dependency tree, the permission
of any ``require_permission`` dependency, and the endpoint's source file and
source text.
"""

from __future__ import annotations

import contextlib
import inspect
import json
import os
import sys
import textwrap
from typing import Any, AsyncIterator, Dict, List, Set


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


def _route_entries(app: Any) -> List[Dict[str, Any]]:
    from fastapi.routing import APIRoute

    entries: List[Dict[str, Any]] = []
    for route in app.routes:
        if not isinstance(route, APIRoute):
            continue
        names: Set[str] = set()
        perms: Set[str] = set()
        _dependency_names(route.dependant, names, perms)
        endpoint = inspect.unwrap(route.endpoint)
        for method in sorted(route.methods):
            entries.append(
                {
                    "key": f"{method} {route.path}",
                    "dependencies": sorted(names),
                    "permissions": sorted(perms),
                    "file": inspect.getsourcefile(endpoint),
                    "line": inspect.getsourcelines(endpoint)[1],
                    "source": textwrap.dedent(inspect.getsource(endpoint)),
                }
            )
    return entries


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
    # The one router the lifespan mounts, unconditionally, at startup.
    from code_indexer.server.auth.oidc import routes as oidc_routes

    app.include_router(oidc_routes.router)
    with open(sys.argv[1], "w", encoding="utf-8") as out:
        json.dump(_route_entries(app), out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
