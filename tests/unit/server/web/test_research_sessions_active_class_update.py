"""
Unit tests for Bug #150 Regression: Session active class not updated client-side.

Tests verify that when a session is clicked, the client-side JavaScript properly
updates the 'active' class to ensure messages are sent to the correct session.

Root Cause:
- Server-side sets active class via Jinja
- Client-side HTMX loads messages but does NOT update sessions list HTML
- Old session keeps .active class
- hx-on::before-request reads wrong session from .session-item.active

Fix:
Add client-side JavaScript in hx-on::before-request to:
1. Remove 'active' class from all .session-item elements
2. Add 'active' class to the clicked session
3. Update hidden form field with session ID

Acceptance Criteria:
- AC1: hx-on::before-request contains querySelectorAll to find all session items
- AC2: hx-on::before-request removes 'active' class from all sessions
- AC3: hx-on::before-request adds 'active' class to clicked session (this)
- AC4: Active session ID hidden field is still updated correctly
- AC5: All session items have the updated hx-on::before-request code
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
        {
            "id": "session-3-id",
            "name": "Test Session 3",
            "folder_path": "/path/to/session3",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
    ]


class TestResearchSessionsActiveClassUpdate:
    """Test client-side active class updates for Bug #150."""

    def test_hx_on_before_request_has_queryselectorall(self, jinja_env, sample_sessions):
        """
        AC1: hx-on::before-request contains querySelectorAll to find all session items.

        Verifies that the event handler queries for all .session-item elements
        to remove the active class from them.
        """
        template = jinja_env.get_template("partials/research_sessions_list.html")
        html = template.render(sessions=sample_sessions, active_session_id="session-1-id")

        # Verify hx-on::before-request exists
        assert 'hx-on::before-request=' in html, \
            "Session items must have hx-on::before-request handler"

        # Verify it contains querySelectorAll for session items
        assert "querySelectorAll('.session-item')" in html or 'querySelectorAll(".session-item")' in html, \
            "hx-on::before-request must use querySelectorAll to find all session items"

    def test_hx_on_before_request_removes_active_class(self, jinja_env, sample_sessions):
        """
        AC2: hx-on::before-request removes 'active' class from all sessions.

        Verifies that the event handler iterates through all session items
        and removes the 'active' class using classList.remove.
        """
        template = jinja_env.get_template("partials/research_sessions_list.html")
        html = template.render(sessions=sample_sessions, active_session_id="session-1-id")

        # Extract hx-on::before-request content
        session_item_sections = html.split('class="session-item')

        # Check first session item (should have the handler)
        assert len(session_item_sections) > 1, "Must have session items"

        first_session = session_item_sections[1]
        assert 'hx-on::before-request=' in first_session, \
            "Session item must have hx-on::before-request"

        # Verify it contains forEach to iterate
        assert 'forEach' in html, \
            "hx-on::before-request must use forEach to iterate through sessions"

        # Verify it removes active class
        assert "classList.remove('active')" in html or 'classList.remove("active")' in html, \
            "hx-on::before-request must call classList.remove('active') on each session"

    def test_hx_on_before_request_adds_active_to_clicked(self, jinja_env, sample_sessions):
        """
        AC3: hx-on::before-request adds 'active' class to clicked session.

        Verifies that after removing active from all sessions, the handler
        adds the active class to 'this' (the clicked session).
        """
        template = jinja_env.get_template("partials/research_sessions_list.html")
        html = template.render(sessions=sample_sessions, active_session_id="session-1-id")

        # Verify it adds active class to this element
        assert "this.classList.add('active')" in html or 'this.classList.add("active")' in html, \
            "hx-on::before-request must call this.classList.add('active') to mark clicked session"

    def test_hx_on_before_request_updates_hidden_field(self, jinja_env, sample_sessions):
        """
        AC4: Active session ID hidden field is still updated correctly.

        Verifies that the existing functionality to update the hidden field
        is preserved after adding class manipulation code.
        """
        template = jinja_env.get_template("partials/research_sessions_list.html")
        html = template.render(sessions=sample_sessions, active_session_id="session-1-id")

        # Verify hidden field update is still present
        assert "getElementById('active-session-id')" in html, \
            "hx-on::before-request must still update active-session-id hidden field"

        assert ".value = " in html, \
            "hx-on::before-request must still set the value of hidden field"

    def test_correct_execution_order_in_hx_on_before_request(self, jinja_env, sample_sessions):
        """
        Verify correct execution order in hx-on::before-request handler.

        Order must be:
        1. Remove active class from all sessions
        2. Add active class to clicked session
        3. Update hidden field value
        """
        template = jinja_env.get_template("partials/research_sessions_list.html")
        html = template.render(sessions=sample_sessions, active_session_id="session-1-id")

        # Extract the handler content
        handler_start = html.find('hx-on::before-request="')
        assert handler_start != -1, "Must have hx-on::before-request attribute"

        handler_start += len('hx-on::before-request="')
        handler_end = html.find('"', handler_start)
        handler_content = html[handler_start:handler_end]

        # Find positions of key operations - use explicit conditionals to avoid find() edge cases
        remove_pos = handler_content.find("classList.remove('active')")
        if remove_pos == -1:
            remove_pos = handler_content.find('classList.remove("active")')

        add_pos = handler_content.find("classList.add('active')")
        if add_pos == -1:
            add_pos = handler_content.find('classList.add("active")')

        update_pos = handler_content.find("getElementById('active-session-id')")

        # All operations must exist
        assert remove_pos != -1, "Must remove active class"
        assert add_pos != -1, "Must add active class"
        assert update_pos != -1, "Must update hidden field"

        # Verify order: remove < add < update
        assert remove_pos < add_pos, \
            "Must remove active from all sessions BEFORE adding to clicked session"
        assert add_pos < update_pos, \
            "Must add active to clicked session BEFORE updating hidden field"

    def test_all_sessions_have_updated_handler(self, jinja_env, sample_sessions):
        """
        AC5: All session items have the updated hx-on::before-request code.

        Verifies that EVERY rendered session item (not just the first one)
        includes the class manipulation code in its handler.
        """
        template = jinja_env.get_template("partials/research_sessions_list.html")
        html = template.render(sessions=sample_sessions, active_session_id="session-1-id")

        # Count session items
        session_count = html.count('class="session-item')
        assert session_count == len(sample_sessions), \
            f"Must render {len(sample_sessions)} session items"

        # Count hx-on::before-request occurrences
        handler_count = html.count('hx-on::before-request=')
        assert handler_count == len(sample_sessions), \
            f"Each session item must have hx-on::before-request handler"

        # Each handler should contain class manipulation code
        # Count querySelectorAll occurrences (one per handler)
        query_count = html.count("querySelectorAll('.session-item')") + html.count('querySelectorAll(".session-item")')
        assert query_count == len(sample_sessions), \
            f"Each handler must contain querySelectorAll for class manipulation"
