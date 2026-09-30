"""
Structural tests for the global plain-HTML form submit interceptor.

The interceptor routes /admin/ (and /user/) POST forms through fetch() so the
elevation 403 interceptor can open the TOTP modal instead of showing raw
JSON. The interceptor logic itself lives in the shared
`static/js/elevation_interceptor.js` file, loaded via a <script src=...> tag
from both base.html (admin pages) and user_base.html (self-service pages) --
it is no longer inlined in either template.

Tests verify the JS scaffolding is present in the shared script, and that
both templates actually include it, and that the real app serves it through
the static mount -- no browser automation required; interactive behaviour is
validated by manual testing.
"""

import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


_PROJECT_ROOT = Path(__file__).resolve().parents[4]

_INTERCEPTOR_JS = (
    _PROJECT_ROOT
    / "src"
    / "code_indexer"
    / "server"
    / "web"
    / "static"
    / "js"
    / "elevation_interceptor.js"
)

_BASE_HTML = (
    _PROJECT_ROOT
    / "src"
    / "code_indexer"
    / "server"
    / "web"
    / "templates"
    / "base.html"
)

_USER_BASE_HTML = (
    _PROJECT_ROOT
    / "src"
    / "code_indexer"
    / "server"
    / "web"
    / "templates"
    / "user_base.html"
)

# A script tag naming the shared interceptor file, tolerant of the
# "?v={{ static_version }}" cache-busting query string each template appends.
_SCRIPT_TAG_PATTERN = re.compile(
    r'<script\s+src="[^"]*elevation_interceptor\.js[^"]*"\s*></script>'
)


def _read_js() -> str:
    return _INTERCEPTOR_JS.read_text(encoding="utf-8")


def _read_base() -> str:
    return _BASE_HTML.read_text(encoding="utf-8")


def _read_user_base() -> str:
    return _USER_BASE_HTML.read_text(encoding="utf-8")


def _includes_interceptor_script_tag(html: str) -> bool:
    """True when `html` contains a <script src=...> tag naming the shared
    elevation_interceptor.js file (not merely a comment mentioning the
    filename)."""
    return _SCRIPT_TAG_PATTERN.search(html) is not None


# ---------------------------------------------------------------------------
# Tests: interceptor scaffolding lives in the shared JS file
# ---------------------------------------------------------------------------


def test_should_intercept_present():
    """_shouldIntercept function must be defined in the form interceptor IIFE."""
    content = _read_js()
    assert "_shouldIntercept" in content, (
        "elevation_interceptor.js is missing the _shouldIntercept function "
        "required by the global form submit interceptor."
    )


def test_prototype_submit_override_present():
    """HTMLFormElement.prototype.submit must be overridden to intercept programmatic .submit() calls."""
    content = _read_js()
    assert "HTMLFormElement.prototype.submit" in content, (
        "elevation_interceptor.js is missing the HTMLFormElement.prototype.submit "
        "override required to intercept programmatic form.submit() calls."
    )


def test_bubble_phase_submit_listener_present():
    """Submit event listener must use bubble phase (third arg false) so
    inline onsubmit attribute handlers fire before the interceptor.

    Uses re.search with re.DOTALL to match the submit listener from
    addEventListener('submit', through }, false) as a single expression,
    ensuring the false third argument belongs to the submit listener itself.
    """
    content = _read_js()
    pattern = r"addEventListener\(['\"]submit['\"],\s*function\s*\([^)]*\)\s*\{.*?\},\s*false\)"
    assert re.search(pattern, content, re.DOTALL) is not None, (
        "elevation_interceptor.js submit listener does not use bubble phase. "
        "Expected addEventListener('submit', function(...) { ... }, false) "
        "but the pattern was not found."
    )


def test_do_submit_checks_cidx_redirecting_guard():
    """Bug #1017: _doSubmit must check window._cidxRedirecting before processing
    the response body, to prevent document.write from cancelling a pending
    totp_setup_required redirect initiated by the fetch() interceptor.

    The guard line 'if (window._cidxRedirecting) return;' must appear inside
    the _doSubmit .then() handler, strictly before the resp.text() call that
    would destroy the DOM via document.write().
    """
    content = _read_js()
    # Locate _doSubmit function body and verify the guard precedes resp.text()
    pattern = (
        r"function\s+_doSubmit\s*\([^)]*\)\s*\{.*?"
        r"if\s*\(\s*window\._cidxRedirecting\s*\)\s*return\s*;.*?"
        r"resp\.text\s*\(\s*\)"
    )
    assert re.search(pattern, content, re.DOTALL) is not None, (
        "elevation_interceptor.js _doSubmit() is missing the window._cidxRedirecting "
        "guard before resp.text(). This guard prevents document.write from "
        "cancelling a totp_setup_required redirect (Bug #1017)."
    )


# ---------------------------------------------------------------------------
# Tests: both templates actually include the shared script
# ---------------------------------------------------------------------------


def test_base_html_includes_shared_interceptor_script_tag():
    """base.html (admin pages) must load the shared interceptor via <script src=...>,
    not inline it."""
    content = _read_base()
    assert _includes_interceptor_script_tag(content), (
        "base.html does not include a <script src=...> tag for the shared "
        "elevation_interceptor.js file."
    )


def test_user_base_html_includes_shared_interceptor_script_tag():
    """user_base.html (self-service pages) must load the same shared
    interceptor via <script src=...>, not a separate inline copy."""
    content = _read_user_base()
    assert _includes_interceptor_script_tag(content), (
        "user_base.html does not include a <script src=...> tag for the "
        "shared elevation_interceptor.js file."
    )


# ---------------------------------------------------------------------------
# Test: the real app actually serves the shared script through the static mount
# ---------------------------------------------------------------------------


def _get_app(tmpdir: str):
    """Lazy-import the real app with an isolated data dir (the established
    pattern used across this test suite's elevation-gate coverage)."""
    from unittest.mock import patch

    from code_indexer.server.services.config_service import reset_config_service

    with patch.dict(
        "os.environ",
        {"CIDX_SERVER_DATA_DIR": tmpdir, "CIDX_DATA_DIR": tmpdir},
    ):
        reset_config_service()
        from code_indexer.server.app import app as _app

        return _app


@pytest.fixture
def isolated_app(tmp_path, monkeypatch):
    monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("CIDX_DATA_DIR", str(tmp_path))
    return _get_app(str(tmp_path))


def test_static_mount_serves_the_shared_interceptor_script(isolated_app):
    """The real app must serve elevation_interceptor.js as JavaScript through
    the /admin/static mount both templates reference -- proving the file is
    actually reachable through the front door, not merely present on disk."""
    client = TestClient(isolated_app)

    response = client.get("/admin/static/js/elevation_interceptor.js")

    assert response.status_code == 200, response.text
    content_type = response.headers.get("content-type", "")
    assert "javascript" in content_type.lower(), (
        f"expected a JavaScript content type, got {content_type!r}"
    )
