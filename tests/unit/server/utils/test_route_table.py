"""iter_routes lists every route an app or router serves at its full path,
whether FastAPI copies an included router's routes (before 0.16x) or keeps
the router nested (``_IncludedRouter``, FastAPI 0.16x)."""

from __future__ import annotations

from fastapi import APIRouter, FastAPI
from fastapi.routing import APIRoute

from code_indexer.server.utils.route_table import iter_routes


def _nested_app() -> FastAPI:
    leaf = APIRouter()
    leaf.add_api_route("/x", lambda: 1, methods=["POST"])
    middle = APIRouter()
    middle.include_router(leaf, prefix="/a")
    middle.add_api_route("/y", lambda: 2, methods=["GET"])
    app = FastAPI()
    app.include_router(middle, prefix="/b")
    app.add_api_route("/z", lambda: 3, methods=["PUT"])
    return app


def test_nested_routes_are_listed_at_their_full_path() -> None:
    served = {
        (path, method)
        for path, route in iter_routes(_nested_app().routes)
        if isinstance(route, APIRoute)
        for method in route.methods
    }
    assert {("/b/a/x", "POST"), ("/b/y", "GET"), ("/z", "PUT")} <= served


def test_a_router_lists_its_own_routes_without_a_prefix() -> None:
    leaf = APIRouter()
    leaf.add_api_route("/x", lambda: 1, methods=["POST"])
    router = APIRouter()
    router.include_router(leaf, prefix="/a")
    assert [path for path, _ in iter_routes(router.routes)] == ["/a/x"]
