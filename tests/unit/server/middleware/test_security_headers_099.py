"""SecurityHeadersMiddleware: security headers on every HTTP response.

Covers the middleware on its own (raw ASGI, so streaming can be observed
message by message) and inside a small FastAPI app (HTML, JSON, redirect,
404, wiki paths, http/https scheme).
"""

import asyncio
from typing import Any, Dict, List

import pytest
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.testclient import TestClient
from starlette.background import BackgroundTask
from starlette.types import Message

from code_indexer.server.middleware.security_headers import (
    FRAME_ANCESTORS_CSP,
    HSTS_VALUE,
    WIKI_CSP,
    SecurityHeadersMiddleware,
)

_BASE = {
    "x-frame-options": "DENY",
    "x-content-type-options": "nosniff",
    "referrer-policy": "same-origin",
}


def _app() -> FastAPI:
    app = FastAPI()

    @app.get("/page", response_class=HTMLResponse)
    def page() -> str:
        return "<html><body>ok</body></html>"

    @app.get("/api/thing")
    def thing() -> Dict[str, str]:
        return {"ok": "yes"}

    @app.get("/go")
    def go() -> RedirectResponse:
        return RedirectResponse("/page", status_code=303)

    @app.get("/own-headers")
    def own_headers() -> Response:
        return Response(
            "x",
            headers={
                "Referrer-Policy": "no-referrer",
                "Content-Security-Policy": "frame-ancestors 'self'",
            },
        )

    @app.get("/wiki/{rest:path}")
    def wiki(rest: str) -> HTMLResponse:
        return HTMLResponse("<html></html>")

    app.add_middleware(SecurityHeadersMiddleware)
    return app


@pytest.fixture
def client() -> TestClient:
    return TestClient(_app(), follow_redirects=False)


def _assert_base(resp: Any, csp: str = FRAME_ANCESTORS_CSP) -> None:
    for name, value in _BASE.items():
        assert resp.headers.get_list(name) == [value], name
    assert resp.headers.get_list("content-security-policy") == [csp]


@pytest.mark.parametrize(
    "path,status",
    [("/page", 200), ("/api/thing", 200), ("/go", 303), ("/missing", 404)],
    ids=["html", "json", "redirect", "not-found"],
)
def test_every_response_carries_base_headers(client, path, status):
    resp = client.get(path)
    assert resp.status_code == status
    _assert_base(resp)


def test_headers_a_route_set_are_kept(client):
    resp = client.get("/own-headers")
    assert resp.headers.get_list("referrer-policy") == ["no-referrer"]
    assert resp.headers.get_list("content-security-policy") == [
        "frame-ancestors 'self'"
    ]
    assert resp.headers.get_list("x-frame-options") == ["DENY"]


@pytest.mark.parametrize("path", ["/wiki/repo/", "/wiki/_static/wiki.css"])
def test_wiki_responses_carry_the_strict_csp(client, path):
    resp = client.get(path)
    _assert_base(resp, csp=WIKI_CSP)


def test_wiki_csp_restricts_script_and_framing():
    directives = {d.strip().split(" ", 1)[0]: d.strip() for d in WIKI_CSP.split(";")}
    assert directives["script-src"] == "script-src 'self'"
    assert directives["default-src"] == "default-src 'self'"
    assert directives["object-src"] == "object-src 'none'"
    assert directives["base-uri"] == "base-uri 'none'"
    assert directives["frame-ancestors"] == "frame-ancestors 'none'"


def test_non_wiki_prefix_lookalike_gets_base_csp(client):
    resp = client.get("/wikipedia")
    assert resp.headers["content-security-policy"] == FRAME_ANCESTORS_CSP


def test_hsts_only_for_https_scheme():
    app = _app()
    https = TestClient(app, base_url="https://testserver").get("/page")
    plain = TestClient(app, base_url="http://testserver").get("/page")
    assert https.headers.get_list("strict-transport-security") == [HSTS_VALUE]
    assert "strict-transport-security" not in plain.headers


def test_background_task_still_runs():
    ran: List[bool] = []
    app = FastAPI()

    @app.get("/bg")
    def bg() -> Response:
        return Response("x", background=BackgroundTask(lambda: ran.append(True)))

    app.add_middleware(SecurityHeadersMiddleware)
    resp = TestClient(app).get("/bg")
    assert resp.status_code == 200
    assert ran == [True]


def test_streaming_body_is_forwarded_chunk_by_chunk():
    """The first chunk reaches the server before the app produces the second.

    A buffering middleware would hold chunk one until the app finished, and
    the app waits for chunk one to arrive -- that deadlocks into the timeout.
    """
    sent: List[Message] = []

    async def _scenario() -> None:
        # Built inside the running loop (Python 3.9 binds an Event to it).
        first_chunk_sent = asyncio.Event()

        async def streaming_app(scope: Any, receive: Any, send: Any) -> None:
            await send({"type": "http.response.start", "status": 200, "headers": []})
            await send(
                {"type": "http.response.body", "body": b"one", "more_body": True}
            )
            await first_chunk_sent.wait()
            await send({"type": "http.response.body", "body": b"two"})

        async def server_send(message: Message) -> None:
            sent.append(message)
            if message.get("body") == b"one":
                first_chunk_sent.set()

        async def receive() -> Message:
            return {"type": "http.request", "body": b"", "more_body": False}

        scope = {"type": "http", "path": "/stream", "scheme": "http", "headers": []}
        await asyncio.wait_for(
            SecurityHeadersMiddleware(streaming_app)(scope, receive, server_send), 5
        )

    asyncio.run(_scenario())
    assert [m.get("body") for m in sent[1:]] == [b"one", b"two"]
    start_headers = dict(sent[0]["headers"])
    assert start_headers[b"x-frame-options"] == b"DENY"


def test_non_http_scopes_pass_through_untouched():
    seen: List[str] = []

    async def inner(scope: Any, receive: Any, send: Any) -> None:
        seen.append(scope["type"])
        await send({"type": "websocket.accept"})

    out: List[Message] = []

    async def send(message: Message) -> None:
        out.append(message)

    async def receive() -> Message:
        return {}

    asyncio.run(SecurityHeadersMiddleware(inner)({"type": "websocket"}, receive, send))
    assert seen == ["websocket"]
    assert out == [{"type": "websocket.accept"}]
