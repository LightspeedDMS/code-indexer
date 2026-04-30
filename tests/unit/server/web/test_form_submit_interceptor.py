"""
Structural tests for the global plain-HTML form submit interceptor in base.html.

The interceptor routes /admin/ POST forms through fetch() so the elevation 403
interceptor can open the TOTP modal instead of showing raw JSON.

Tests verify the JS scaffolding is present in the template — no browser
automation required; behaviour is validated by manual testing.
"""

import re
from pathlib import Path


_BASE_HTML = (
    Path(__file__).resolve().parents[4]
    / "src"
    / "code_indexer"
    / "server"
    / "web"
    / "templates"
    / "base.html"
)


def _read_base() -> str:
    return _BASE_HTML.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_should_intercept_present():
    """_shouldIntercept function must be defined in the form interceptor IIFE."""
    content = _read_base()
    assert "_shouldIntercept" in content, (
        "base.html is missing the _shouldIntercept function required by the "
        "global form submit interceptor."
    )


def test_prototype_submit_override_present():
    """HTMLFormElement.prototype.submit must be overridden to intercept programmatic .submit() calls."""
    content = _read_base()
    assert "HTMLFormElement.prototype.submit" in content, (
        "base.html is missing the HTMLFormElement.prototype.submit override "
        "required to intercept programmatic form.submit() calls."
    )


def test_bubble_phase_submit_listener_present():
    """Submit event listener must use bubble phase (third arg false) so
    inline onsubmit attribute handlers fire before the interceptor.

    Uses re.search with re.DOTALL to match the submit listener from
    addEventListener('submit', through }, false) as a single expression,
    ensuring the false third argument belongs to the submit listener itself.
    """
    content = _read_base()
    pattern = (
        r"addEventListener\(['\"]submit['\"],\s*function\s*\([^)]*\)\s*\{.*?\},\s*false\)"
    )
    assert re.search(pattern, content, re.DOTALL) is not None, (
        "base.html submit listener does not use bubble phase. "
        "Expected addEventListener('submit', function(...) { ... }, false) "
        "but the pattern was not found."
    )
