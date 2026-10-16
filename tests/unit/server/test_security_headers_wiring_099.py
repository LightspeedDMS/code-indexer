"""The real app (create_app) sends the security headers on every response.

Drives a real ``create_app()`` app over an isolated server home with no
credentials, through the HTTP front door.
"""

import importlib
from pathlib import Path
from typing import Any, Iterator, Tuple

import pytest
from fastapi.testclient import TestClient

from code_indexer.server.middleware.security_headers import (
    FRAME_ANCESTORS_CSP,
    HSTS_VALUE,
    WIKI_CSP,
)
from tests.unit.server._isolated_app import isolated_app

INLINE_MISC_MODULE_PATH = "code_indexer.server.routers.inline_misc"
DB_HEALTH_MODULE_PATH = "code_indexer.server.services.database_health_service"

_BASE = {
    "x-frame-options": "DENY",
    "x-content-type-options": "nosniff",
    "referrer-policy": "same-origin",
}


@pytest.fixture(scope="module")
def clients(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[Tuple[TestClient, TestClient]]:
    """(http client, https client) over one isolated real app."""
    root: Path = tmp_path_factory.mktemp("security-headers-wiring")
    inline_misc = importlib.import_module(INLINE_MISC_MODULE_PATH)
    db_health = importlib.import_module(DB_HEALTH_MODULE_PATH)
    inline_misc._reset_healthz_cache()
    try:
        with isolated_app(root) as app:
            db_health._reset_singleton_for_testing()
            yield (
                TestClient(app, follow_redirects=False),
                TestClient(app, base_url="https://testserver", follow_redirects=False),
            )
    finally:
        inline_misc._reset_healthz_cache()


def _assert_base(resp: Any, csp: str = FRAME_ANCESTORS_CSP) -> None:
    for name, value in _BASE.items():
        assert resp.headers.get_list(name) == [value], (name, resp.headers)
    assert resp.headers.get_list("content-security-policy") == [csp]


def test_html_page_carries_headers(clients):
    http, _ = clients
    resp = http.get("/login")
    assert resp.status_code == 200
    assert "text/html" in resp.headers["content-type"]
    _assert_base(resp)


def test_healthz_json_carries_headers_and_body_unchanged(clients):
    http, _ = clients
    resp = http.get("/healthz")
    assert resp.status_code in {200, 503}
    assert set(resp.json().keys()) == {"status"}
    _assert_base(resp)


def test_redirect_carries_headers(clients):
    http, _ = clients
    resp = http.get("/docs")
    assert resp.status_code == 303
    _assert_base(resp)


def test_not_found_carries_headers(clients):
    http, _ = clients
    resp = http.get("/no-such-route-099")
    assert resp.status_code == 404
    _assert_base(resp)


def test_wiki_static_carries_strict_csp(clients):
    http, _ = clients
    resp = http.get("/wiki/_static/wiki.css")
    assert resp.status_code == 200
    _assert_base(resp, csp=WIKI_CSP)


def test_wiki_theme_script_is_served(clients):
    http, _ = clients
    resp = http.get("/wiki/_static/wiki-theme.js")
    assert resp.status_code == 200
    assert "javascript" in resp.headers["content-type"]
    assert "wiki_theme" in resp.text
    _assert_base(resp, csp=WIKI_CSP)


def test_hsts_only_when_scheme_is_https(clients):
    http, https = clients
    assert https.get("/healthz").headers.get_list("strict-transport-security") == [
        HSTS_VALUE
    ]
    assert "strict-transport-security" not in http.get("/healthz").headers
