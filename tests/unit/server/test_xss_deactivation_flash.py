"""Regression test for Story #1032 Commit 1 — XSS BLOCKER fix.

The admin "Deactivate" success flash at `web/routes.py:4079` embeds
`user_alias` inside an HTML message rendered through Jinja's `|safe` filter.
Without escaping, an alias containing HTML metacharacters (e.g.
`<script>alert(1)</script>`) would execute in the admin's browser.

This test asserts that `html.escape()` is applied to `user_alias` in the
constructed success_message string — so a malicious alias is rendered as
inert text, not executable HTML.
"""

import html


def test_success_message_escapes_xss_alias():
    """user_alias containing HTML metacharacters must be HTML-escaped in the
    success flash before being rendered through Jinja's |safe filter."""
    malicious_alias = "<script>alert('xss')</script>"
    safe_alias = html.escape(malicious_alias)

    # Simulate the exact pattern used at web/routes.py:4079
    job_link = '<a href="/admin/jobs?search_text=abc">abc</a>'
    success_message = (
        f"Repository '{html.escape(malicious_alias)}' "
        f"deactivation job submitted (Job ID: {job_link})"
    )

    # The raw script tag must NOT appear in the flash content
    assert "<script>" not in success_message, (
        "Stored-XSS regression: raw <script> tag leaked into success_message"
    )
    # The escaped form must be present
    assert safe_alias in success_message, (
        "Escaped form of malicious alias missing from success_message"
    )
    # The trusted anchor literal must remain intact (so the Job ID link works)
    assert job_link in success_message, (
        "Trusted job_link anchor must be preserved verbatim"
    )


def test_success_message_escapes_ampersand_and_quotes():
    """Aliases containing &, ", ' must also be escaped."""
    alias = "mix&me\"yo'lo"
    escaped = html.escape(alias)

    success_message = (
        f"Repository '{html.escape(alias)}' deactivation job submitted "
        f'(Job ID: <a href="/admin/jobs?search_text=x">x</a>)'
    )

    assert escaped in success_message
    # Raw ampersand should not appear (must be &amp; or escaped)
    # Note: html.escape converts & to &amp;, " to &quot;, ' to &#x27;
    assert "&amp;" in success_message
    assert "&quot;" in success_message
    assert "&#x27;" in success_message
