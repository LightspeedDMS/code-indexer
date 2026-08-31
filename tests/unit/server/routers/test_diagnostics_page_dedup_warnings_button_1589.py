"""
Story #1589 AC7: the Diagnostics tab must render a "Clear All Dedup
Warnings" button whose click handler shows a confirmation dialog BEFORE
making any API call, and on confirmation calls the clear-all endpoint.

There is no JS test harness in this project (no package.json, no
jest/playwright config -- verified before writing this test), so this is
the level of automated verification available: render the REAL page
template through the REAL router (exactly as a browser would receive it)
and assert the emitted HTML/JS contains the required button wired to its
handler, the exact `if (!window.confirm(...)) { return; }`
early-return-on-cancel guard, and a `fetch(...)` call whose literal first
argument is EXACTLY the clear-all endpoint path -- proving the wiring a
human reviewer would otherwise have to inspect by eye. Mirrors
test_diagnostics_router.py's exact fixture pattern.
"""

import re
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from code_indexer.server.routers.diagnostics import router

_ELEVATION_QUALNAME = "require_elevation.<locals>._check"
_CLEAR_ALL_PATH = "/api/admin/diagnostics/dedup-warnings/clear-all"

# `if (!window.confirm(<anything>)) { return...; }` -- the exact
# cancel-returns-early idiom this codebase's restartServer() already
# uses. DOTALL so a multi-line confirm() message string still matches.
_CANCEL_GUARD_RE = re.compile(
    r"if\s*\(\s*!\s*window\.confirm\(.*?\)\s*\)\s*\{\s*return\b",
    re.DOTALL,
)

# `fetch('<literal-path>'` / `fetch("<literal-path>"` -- captures the
# literal string argument so it can be compared for EXACT equality,
# never mere substring containment.
_FETCH_CALL_RE = re.compile(r"fetch\(\s*['\"]([^'\"]+)['\"]")


def _bypass_elevation(app, router):
    for route in router.routes:
        if not isinstance(route, APIRoute):
            continue
        for dep in route.dependencies or []:
            dep_callable = getattr(dep, "dependency", None)
            if (
                dep_callable
                and getattr(dep_callable, "__qualname__", "") == _ELEVATION_QUALNAME
            ):
                app.dependency_overrides[dep_callable] = lambda: None


@pytest.fixture
def client():
    app = FastAPI()
    app.include_router(router)
    _bypass_elevation(app, router)
    return TestClient(app)


@pytest.fixture(autouse=True)
def mock_csrf_cookie():
    """Mirrors test_diagnostics_router.py's TestDiagnosticsPageEndpoint.
    mock_csrf_cookie fixture exactly: without a wired session manager
    (not present in this minimal test app), set_csrf_cookie's real
    implementation raises "Session manager not initialized" and the page
    render 500s -- a pre-existing test-environment gap unrelated to what
    this test file verifies (the dedup-warnings button and its handler)."""
    with patch("code_indexer.server.routers.diagnostics.set_csrf_cookie"):
        yield


def _extract_handler_body(html: str) -> str:
    """The clearAllDedupWarnings() function body, bounded by brace
    matching from its own declaration to its OWN closing brace -- never
    an arbitrary character slice or the whole rest of the <script> block,
    so a later unrelated handler in the same script cannot leak into
    scope for these assertions. Fails loudly (via pytest.fail, never a
    silent mis-slice) if the function or a balanced brace pair is
    missing."""
    marker = "function clearAllDedupWarnings"
    start = html.find(marker)
    if start == -1:
        pytest.fail(
            f"Rendered page has no `{marker}` JS function -- the "
            "clearAllDedupWarnings() click handler is missing entirely."
        )
    open_brace = html.find("{", start)
    if open_brace == -1:
        pytest.fail(
            f"`{marker}` has no opening brace `{{` -- malformed function declaration."
        )
    depth = 0
    pos = open_brace
    for pos in range(open_brace, len(html)):
        char = html[pos]
        if char == "{":
            depth += 1
        elif char == "}":
            depth -= 1
            if depth == 0:
                break
    if depth != 0:
        pytest.fail(
            f"`{marker}`'s braces never balance to 0 -- malformed/unclosed "
            "function body."
        )
    return html[start : pos + 1]


class TestDiagnosticsPageRendersClearAllDedupWarningsButton:
    def test_page_contains_the_button_wired_to_its_handler(self, client):
        response = client.get("/admin/diagnostics")
        assert response.status_code == 200
        assert 'id="clear-dedup-warnings-btn"' in response.text
        assert "Clear All Dedup Warnings" in response.text
        assert 'onclick="clearAllDedupWarnings()"' in response.text, (
            "the button must be wired to its click handler via onclick"
        )


class TestClearAllDedupWarningsHandlerConfirmsBeforeFetch:
    def test_cancelling_confirm_returns_before_fetch(self, client):
        response = client.get("/admin/diagnostics")
        handler_body = _extract_handler_body(response.text)

        guard_match = _CANCEL_GUARD_RE.search(handler_body)
        assert guard_match is not None, (
            "handler must contain `if (!window.confirm(...)) { return; }` "
            "-- the exact cancel-returns-early guard, not merely an "
            "unrelated confirm()/return pair"
        )

        fetch_match = _FETCH_CALL_RE.search(handler_body)
        assert fetch_match is not None, "handler must call fetch(...)"
        assert guard_match.start() < fetch_match.start(), (
            "the cancel guard must appear BEFORE the fetch(...) call"
        )

    def test_fetch_call_targets_the_exact_clear_all_endpoint(self, client):
        response = client.get("/admin/diagnostics")
        handler_body = _extract_handler_body(response.text)

        fetch_match = _FETCH_CALL_RE.search(handler_body)
        assert fetch_match is not None
        assert fetch_match.group(1) == _CLEAR_ALL_PATH
