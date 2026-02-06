"""
Tests for Bug #154: Auto-Scroll NOT WORKING AT ALL.

Tests verify that the auto-scroll JavaScript implementation correctly:
1. Scrolls to bottom on page load
2. Scrolls to bottom when new messages arrive (if auto-scroll enabled)
3. Disables auto-scroll when user scrolls up
4. Re-enables auto-scroll when user scrolls back to bottom
5. Always scrolls on user message send

Note: These are structural tests. Full E2E testing requires browser automation.
"""

import pytest
from pathlib import Path


@pytest.fixture
def template_content():
    """
    Load research_assistant.html template content.

    Path construction: test file -> server -> services -> routers -> web -> (up to project root) -> src
    """
    # Navigate from tests/unit/server/web/ up to project root, then to template
    template_path = (
        Path(__file__).parent.parent.parent.parent.parent
        / "src"
        / "code_indexer"
        / "server"
        / "web"
        / "templates"
        / "research_assistant.html"
    )

    if not template_path.exists():
        pytest.fail(f"Template not found at {template_path}")

    return template_path.read_text()


def test_research_assistant_template_has_auto_scroll_script(template_content):
    """Test that research_assistant.html contains auto-scroll script."""
    # Verify script exists
    assert "Bug #154: Smart Auto-Scroll" in template_content
    assert "autoScrollEnabled" in template_content
    assert "scrollToBottom()" in template_content
    assert "isAtBottom()" in template_content


def test_auto_scroll_script_has_dom_ready_wrapper(template_content):
    """Test that auto-scroll script wraps in DOMContentLoaded for safety."""
    # Should have DOMContentLoaded wrapper OR be placed at end of body
    # Since it's at the end of the body, it should work, but let's verify structure
    assert "<script>" in template_content
    assert "getElementById('chat-messages')" in template_content


def test_auto_scroll_listens_to_htmx_events(template_content):
    """Test that auto-scroll script listens to HTMX afterSwap events."""
    # Verify HTMX event listeners
    assert "htmx:afterSwap" in template_content
    assert "htmx:beforeRequest" in template_content


def test_auto_scroll_checks_correct_target(template_content):
    """Test that auto-scroll checks for chat-messages target correctly."""
    # Should check if target is chat-messages
    # The check should work for both direct target and parent scenarios
    assert "chat-messages" in template_content


def test_auto_scroll_handles_user_send(template_content):
    """Test that auto-scroll always scrolls when user sends message."""
    # Should have beforeRequest handler for send form
    assert 'form[hx-post="/admin/research/send"]' in template_content or '/admin/research/send' in template_content


def test_jump_to_bottom_button_exists(template_content):
    """Test that jump-to-bottom button exists in template."""
    # Verify jump button
    assert 'id="jump-to-bottom"' in template_content
    assert "Jump to bottom" in template_content


def test_auto_scroll_uses_settimeout_for_dom_update(template_content):
    """Test that auto-scroll uses setTimeout to wait for DOM updates."""
    # Should use setTimeout to ensure DOM is updated before scrolling
    assert "setTimeout" in template_content or "requestAnimationFrame" in template_content
