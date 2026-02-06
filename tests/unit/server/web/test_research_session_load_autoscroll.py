"""
Unit tests for Bug #154c: Auto-scroll to bottom when loading a different session.

Tests verify that when a user clicks on a different session in the Research Assistant
sidebar, the conversation loads AND automatically scrolls to the bottom, even if the
user was scrolled up in the previous session.

Problem:
- Current code checks `if (autoScrollEnabled)` before scrolling after swap
- If user was scrolled up in previous session, autoScrollEnabled = false
- When new session loads via HTMX swap, it doesn't scroll

Solution (Option A - Recommended):
- Add handler for session click (in research_sessions_list.html):
  hx-on::before-request="... ; window.forceScrollOnNextSwap = true;"
- In afterSwap (research_assistant.html):
  if (window.forceScrollOnNextSwap || autoScrollEnabled) {
      scrollToBottom();
      window.forceScrollOnNextSwap = false;
  }

Acceptance Criteria:
- AC1: Session item has hx-on::before-request that sets window.forceScrollOnNextSwap = true
- AC2: The flag is set AFTER existing active class manipulation code
- AC3: Main page htmx:afterSwap checks window.forceScrollOnNextSwap
- AC4: afterSwap resets window.forceScrollOnNextSwap = false after use
- AC5: afterSwap scrolls if forceScrollOnNextSwap OR autoScrollEnabled
"""

import pytest
from jinja2 import Environment, FileSystemLoader
from pathlib import Path
from datetime import datetime, timezone


@pytest.fixture
def jinja_env():
    """Create Jinja2 environment with template path."""
    template_dir = Path(__file__).parent.parent.parent.parent.parent / "src" / "code_indexer" / "server" / "web" / "templates"
    env = Environment(loader=FileSystemLoader(str(template_dir)))

    # Register the relative_time filter
    def relative_time_filter(dt_string):
        """Mock filter that just returns a formatted string."""
        return "2 minutes ago"

    env.filters['relative_time'] = relative_time_filter
    return env


@pytest.fixture
def sample_sessions():
    """Create sample session data for testing."""
    return [
        {
            "id": "session-1-id",
            "name": "Test Session 1",
            "folder_path": "/path/to/session1",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
        {
            "id": "session-2-id",
            "name": "Test Session 2",
            "folder_path": "/path/to/session2",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
    ]


class TestResearchSessionLoadAutoScroll:
    """Test auto-scroll behavior when loading different sessions (Bug #154c)."""

    def test_session_item_sets_force_scroll_flag(self, jinja_env, sample_sessions):
        """
        AC1: Session item has hx-on::before-request that sets window.forceScrollOnNextSwap = true.

        Verifies that clicking a session sets the flag to force scroll on next HTMX swap.
        """
        template = jinja_env.get_template("partials/research_sessions_list.html")
        html = template.render(sessions=sample_sessions, active_session_id="session-1-id")

        # Verify hx-on::before-request exists (already tested in other tests, but verify again)
        assert 'hx-on::before-request=' in html, \
            "Session items must have hx-on::before-request handler"

        # Verify it sets the forceScrollOnNextSwap flag
        assert "window.forceScrollOnNextSwap = true" in html, \
            "hx-on::before-request must set window.forceScrollOnNextSwap = true"

    def test_force_scroll_flag_after_active_class_code(self, jinja_env, sample_sessions):
        """
        AC2: The flag is set AFTER existing active class manipulation code.

        Verifies execution order in hx-on::before-request:
        1. Remove active from all sessions
        2. Add active to clicked session
        3. Update hidden field
        4. Set window.forceScrollOnNextSwap = true
        """
        template = jinja_env.get_template("partials/research_sessions_list.html")
        html = template.render(sessions=sample_sessions, active_session_id="session-1-id")

        # Extract the handler content
        handler_start = html.find('hx-on::before-request="')
        assert handler_start != -1, "Must have hx-on::before-request attribute"

        handler_start += len('hx-on::before-request="')
        handler_end = html.find('"', handler_start)
        handler_content = html[handler_start:handler_end]

        # Find positions of operations
        remove_pos = handler_content.find("classList.remove('active')")
        if remove_pos == -1:
            remove_pos = handler_content.find('classList.remove("active")')

        add_pos = handler_content.find("classList.add('active')")
        if add_pos == -1:
            add_pos = handler_content.find('classList.add("active")')

        update_pos = handler_content.find("getElementById('active-session-id')")

        force_scroll_pos = handler_content.find("window.forceScrollOnNextSwap = true")

        # All operations must exist
        assert remove_pos != -1, "Must remove active class"
        assert add_pos != -1, "Must add active class"
        assert update_pos != -1, "Must update hidden field"
        assert force_scroll_pos != -1, "Must set forceScrollOnNextSwap flag"

        # Verify order: remove < add < update < forceScroll
        assert remove_pos < add_pos, \
            "Must remove active BEFORE adding active"
        assert add_pos < update_pos, \
            "Must add active BEFORE updating hidden field"
        assert update_pos < force_scroll_pos, \
            "Must set forceScrollOnNextSwap AFTER all other operations"

    def test_main_page_after_swap_checks_force_scroll_flag(self, jinja_env):
        """
        AC3: Main page htmx:afterSwap checks window.forceScrollOnNextSwap.

        Verifies that the afterSwap handler in research_assistant.html checks
        the forceScrollOnNextSwap flag before deciding whether to scroll.
        """
        template = jinja_env.get_template("research_assistant.html")
        html = template.render(sessions=[], messages=[], active_session_id=None)

        # Find the htmx:afterSwap event listener
        assert "htmx:afterSwap" in html, \
            "Must have htmx:afterSwap event listener"

        # Extract the afterSwap handler section
        after_swap_start = html.find("htmx:afterSwap")
        assert after_swap_start != -1, "Must find htmx:afterSwap listener"

        # Get a reasonable chunk of the handler (next 1000 chars)
        after_swap_section = html[after_swap_start:after_swap_start + 1000]

        # Verify it checks window.forceScrollOnNextSwap
        assert "window.forceScrollOnNextSwap" in after_swap_section or "forceScrollOnNextSwap" in after_swap_section, \
            "afterSwap handler must check window.forceScrollOnNextSwap flag"

    def test_after_swap_resets_force_scroll_flag(self, jinja_env):
        """
        AC4: afterSwap resets window.forceScrollOnNextSwap = false after use.

        Verifies that after using the flag to scroll, the handler resets it
        to false so it doesn't affect subsequent non-session-load swaps.
        """
        template = jinja_env.get_template("research_assistant.html")
        html = template.render(sessions=[], messages=[], active_session_id=None)

        # Find the htmx:afterSwap handler
        after_swap_start = html.find("htmx:afterSwap")
        assert after_swap_start != -1, "Must find htmx:afterSwap listener"

        # Get handler section
        after_swap_section = html[after_swap_start:after_swap_start + 1500]

        # Verify it resets the flag
        assert "window.forceScrollOnNextSwap = false" in after_swap_section or "forceScrollOnNextSwap = false" in after_swap_section, \
            "afterSwap handler must reset window.forceScrollOnNextSwap = false after use"

    def test_after_swap_scrolls_if_force_flag_or_auto_enabled(self, jinja_env):
        """
        AC5: afterSwap scrolls if forceScrollOnNextSwap OR autoScrollEnabled.

        Verifies that the condition for scrolling checks BOTH flags:
        - Scroll if window.forceScrollOnNextSwap is true (new session load)
        - Scroll if autoScrollEnabled is true (user is at bottom)
        """
        template = jinja_env.get_template("research_assistant.html")
        html = template.render(sessions=[], messages=[], active_session_id=None)

        # Find the htmx:afterSwap handler
        after_swap_start = html.find("htmx:afterSwap")
        assert after_swap_start != -1, "Must find htmx:afterSwap listener"

        # Get handler section (larger to capture full logic)
        after_swap_section = html[after_swap_start:after_swap_start + 1500]

        # Verify conditional check - should be OR logic
        # Looking for patterns like:
        # if (window.forceScrollOnNextSwap || autoScrollEnabled)
        # if (forceScrollOnNextSwap || autoScrollEnabled)

        # First verify both variables are referenced
        has_force_scroll = "forceScrollOnNextSwap" in after_swap_section
        has_auto_scroll = "autoScrollEnabled" in after_swap_section

        assert has_force_scroll, \
            "afterSwap handler must reference forceScrollOnNextSwap in condition"
        assert has_auto_scroll, \
            "afterSwap handler must reference autoScrollEnabled in condition"

        # Verify OR logic exists between them
        # Look for the pattern with OR operator
        assert "||" in after_swap_section or " or " in after_swap_section.lower(), \
            "afterSwap handler must use OR logic to check both flags"

        # More specific check: verify the conditional pattern
        # Should have: (forceScrollOnNextSwap || autoScrollEnabled) or similar
        force_idx = after_swap_section.find("forceScrollOnNextSwap")
        auto_idx = after_swap_section.find("autoScrollEnabled")

        # Both should be in the same conditional region
        assert abs(force_idx - auto_idx) < 200, \
            "forceScrollOnNextSwap and autoScrollEnabled should be in same conditional"

    def test_multiple_sessions_all_set_force_scroll_flag(self, jinja_env):
        """
        Verify all rendered session items set the forceScrollOnNextSwap flag.

        With multiple sessions, each click should set the flag independently.
        """
        # Create 3 sessions
        many_sessions = [
            {
                "id": f"session-{i}",
                "name": f"Session {i}",
                "folder_path": f"/path/{i}",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            for i in range(3)
        ]

        template = jinja_env.get_template("partials/research_sessions_list.html")
        html = template.render(sessions=many_sessions, active_session_id="session-0")

        # Count occurrences of window.forceScrollOnNextSwap = true
        # Should be one per session
        flag_set_count = html.count("window.forceScrollOnNextSwap = true")

        assert flag_set_count == len(many_sessions), \
            f"Expected {len(many_sessions)} forceScrollOnNextSwap flags, found {flag_set_count}"

    def test_scroll_to_bottom_called_in_after_swap(self, jinja_env):
        """
        Verify that scrollToBottom() is actually called in the afterSwap handler.

        The conditional logic should lead to calling scrollToBottom() when either
        flag is true.
        """
        template = jinja_env.get_template("research_assistant.html")
        html = template.render(sessions=[], messages=[], active_session_id=None)

        # Find the htmx:afterSwap handler
        after_swap_start = html.find("htmx:afterSwap")
        assert after_swap_start != -1, "Must find htmx:afterSwap listener"

        # Get handler section
        after_swap_section = html[after_swap_start:after_swap_start + 1500]

        # Verify scrollToBottom() is called
        assert "scrollToBottom()" in after_swap_section, \
            "afterSwap handler must call scrollToBottom() function"
