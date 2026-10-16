"""MFA challenge pages get their framing headers from SecurityHeadersMiddleware."""

import pytest
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient

from code_indexer.server.middleware.security_headers import (
    FRAME_ANCESTORS_CSP,
    SecurityHeadersMiddleware,
)
from code_indexer.server.web.mfa_routes import (
    render_mfa_challenge_page,
    render_oauth_mfa_challenge_page,
)

_RENDERERS = [render_mfa_challenge_page, render_oauth_mfa_challenge_page]
_IDS = ["web", "oauth"]


@pytest.mark.parametrize("render", _RENDERERS, ids=_IDS)
def test_renderer_leaves_framing_headers_to_the_middleware(render):
    page = render("example-token", "")
    assert "content-security-policy" not in page.headers
    assert "x-frame-options" not in page.headers


@pytest.mark.parametrize("render", _RENDERERS, ids=_IDS)
def test_served_page_carries_one_framing_policy(render):
    app = FastAPI()

    @app.get("/challenge")
    def challenge() -> HTMLResponse:
        page: HTMLResponse = render("example-token", "")
        return page

    app.add_middleware(SecurityHeadersMiddleware)
    resp = TestClient(app).get("/challenge")
    assert resp.status_code == 200
    assert resp.headers.get_list("content-security-policy") == [FRAME_ANCESTORS_CSP]
    assert resp.headers.get_list("x-frame-options") == ["DENY"]
