"""
Story #1589: the dedup_warnings_admin router must be actually wired into
the real production app (inline_routes.py's app.include_router calls) --
a router module that only exists as a standalone test double is orphan
code (Messi Rule #12: Anti-Orphan-Code).
"""

from code_indexer.server.app import app
from tests.utils.route_registration import find_route

_ENDPOINT_PATH = "/api/admin/diagnostics/dedup-warnings/clear-all"


class TestDedupWarningsAdminRouterIsWiredIntoRealApp:
    def test_clear_all_route_is_registered_on_the_real_app(self) -> None:
        route = find_route(app, _ENDPOINT_PATH)
        assert route is not None, (
            f"Route {_ENDPOINT_PATH} is not registered on the real app -- "
            "the dedup_warnings_admin router must be included via "
            "app.include_router() in inline_routes.py."
        )
        assert "POST" in route.methods
