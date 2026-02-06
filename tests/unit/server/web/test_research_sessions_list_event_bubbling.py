"""
Unit tests for Research Assistant Sessions List - Event Bubbling Fix.

Tests verify that delete and rename buttons prevent click event bubbling
to parent session-item div (which has hx-get to load session messages).

Bug: Clicking delete/rename buttons was bubbling up to parent div,
causing unwanted GET requests to load the session.

Fix: Added event.stopPropagation() to both buttons.

Acceptance Criteria:
- AC1: Delete button has onclick="event.stopPropagation()"
- AC2: Rename button has event.stopPropagation() in onclick handler
- AC3: Template renders correctly with session data
- AC4: All existing tests pass (no regressions)
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


class TestResearchSessionsListEventBubbling:
    """Test event bubbling prevention in sessions list buttons."""

    def test_delete_button_has_stop_propagation(self, jinja_env, sample_sessions):
        """
        AC1: Delete button has onclick="event.stopPropagation()".

        Verifies that the delete button prevents click events from bubbling
        up to the parent session-item div.
        """
        template = jinja_env.get_template("partials/research_sessions_list.html")
        html = template.render(sessions=sample_sessions, active_session_id="session-1-id")

        # Verify delete button exists
        assert 'class="delete-btn"' in html, "Delete button must exist"

        # Verify delete button has onclick with event.stopPropagation()
        assert 'onclick="event.stopPropagation()"' in html, \
            "Delete button must have onclick='event.stopPropagation()' to prevent bubbling"

        # Verify it's on the button with hx-delete
        # The pattern should be: <button ... hx-delete="..." ... onclick="event.stopPropagation()" ...>
        assert 'hx-delete="/admin/research/sessions/' in html, "Delete button must have hx-delete"

        # More specific check: ensure stopPropagation is on delete button
        delete_btn_section = html.split('class="delete-btn"')[1].split('</button>')[0]
        assert 'onclick="event.stopPropagation()"' in delete_btn_section, \
            "event.stopPropagation() must be on delete button specifically"

    def test_rename_button_has_stop_propagation(self, jinja_env, sample_sessions):
        """
        AC2: Rename button has event.stopPropagation() in onclick handler.

        Verifies that the rename button's onclick handler calls event.stopPropagation()
        BEFORE calling the renameSession function.
        """
        template = jinja_env.get_template("partials/research_sessions_list.html")
        html = template.render(sessions=sample_sessions, active_session_id="session-1-id")

        # Verify rename button exists
        assert 'class="rename-btn"' in html, "Rename button must exist"

        # Verify rename button has onclick with event.stopPropagation()
        # Pattern: onclick="event.stopPropagation(); renameSession(...)"
        assert 'event.stopPropagation()' in html, \
            "Rename button must call event.stopPropagation() in onclick handler"

        # Verify it comes BEFORE renameSession call
        rename_btn_section = html.split('class="rename-btn"')[1].split('</button>')[0]
        assert 'onclick=' in rename_btn_section, "Rename button must have onclick handler"

        onclick_content = rename_btn_section.split('onclick="')[1].split('"')[0]
        assert 'event.stopPropagation()' in onclick_content, \
            "event.stopPropagation() must be in rename button's onclick"
        assert 'renameSession' in onclick_content, \
            "onclick must call renameSession function"

        # Verify order: stopPropagation comes BEFORE renameSession
        stop_idx = onclick_content.index('event.stopPropagation()')
        rename_idx = onclick_content.index('renameSession')
        assert stop_idx < rename_idx, \
            "event.stopPropagation() must be called BEFORE renameSession()"

    def test_template_renders_with_session_data(self, jinja_env, sample_sessions):
        """
        AC3: Template renders correctly with session data.

        Verifies that the template renders all session information correctly
        including names, IDs, and action buttons.
        """
        template = jinja_env.get_template("partials/research_sessions_list.html")
        html = template.render(sessions=sample_sessions, active_session_id="session-1-id")

        # Verify sessions container exists
        assert 'class="sessions-list"' in html, "Sessions list container must exist"

        # Verify all sessions are rendered
        for session in sample_sessions:
            assert session["id"] in html, f"Session {session['id']} must be in HTML"
            assert session["name"] in html, f"Session {session['name']} must be in HTML"

        # Verify session-item divs exist
        assert 'class="session-item' in html, "Session items must be rendered"

        # Verify active class on first session
        assert 'class="session-item active"' in html or 'class="session-item  active"' in html, \
            "Active session must have 'active' class"

    def test_both_buttons_exist_in_rendered_html(self, jinja_env, sample_sessions):
        """
        AC4: Both delete and rename buttons exist in rendered template.

        Verifies that both action buttons are present and properly configured.
        """
        template = jinja_env.get_template("partials/research_sessions_list.html")
        html = template.render(sessions=sample_sessions, active_session_id="session-1-id")

        # Count buttons (should be 2 per session: rename + delete)
        num_sessions = len(sample_sessions)
        assert html.count('class="delete-btn"') == num_sessions, \
            f"Must have {num_sessions} delete buttons (one per session)"
        assert html.count('class="rename-btn"') == num_sessions, \
            f"Must have {num_sessions} rename buttons (one per session)"

    def test_session_item_has_hx_get_attribute(self, jinja_env, sample_sessions):
        """
        Verify session-item div has hx-get to load messages.

        This is the parent div that we're preventing events from bubbling to.
        """
        template = jinja_env.get_template("partials/research_sessions_list.html")
        html = template.render(sessions=sample_sessions, active_session_id="session-1-id")

        # Verify session-item has hx-get
        assert 'hx-get="/admin/research/sessions/' in html, \
            "Session items must have hx-get to load messages"

        # Verify it targets chat-messages
        assert 'hx-target="#chat-messages"' in html, \
            "Session item hx-get must target #chat-messages"

    def test_rename_function_receives_correct_parameters(self, jinja_env, sample_sessions):
        """
        Verify renameSession function is called with correct session ID and name.

        The function should receive session.id and session.name as parameters.
        """
        template = jinja_env.get_template("partials/research_sessions_list.html")
        html = template.render(sessions=sample_sessions, active_session_id="session-1-id")

        # Verify renameSession is called with session data
        for session in sample_sessions:
            expected_call = f"renameSession('{session['id']}', '{session['name']}')"
            assert expected_call in html, \
                f"renameSession must be called with correct parameters for {session['name']}"

    def test_delete_button_has_hx_confirm(self, jinja_env, sample_sessions):
        """
        Verify delete button has confirmation dialog.

        The hx-confirm attribute should prompt user before deletion.
        """
        template = jinja_env.get_template("partials/research_sessions_list.html")
        html = template.render(sessions=sample_sessions, active_session_id="session-1-id")

        # Verify hx-confirm exists on delete button
        delete_btn_section = html.split('class="delete-btn"')[1].split('</button>')[0]
        assert 'hx-confirm=' in delete_btn_section, \
            "Delete button must have hx-confirm for user confirmation"

    def test_delete_button_passes_active_session_id(self, jinja_env, sample_sessions):
        """
        Verify delete button passes active_session_id in hx-vals.

        This allows the server to determine if the deleted session was active
        and update the UI accordingly.
        """
        template = jinja_env.get_template("partials/research_sessions_list.html")
        html = template.render(sessions=sample_sessions, active_session_id="session-1-id")

        # Verify hx-vals contains active_session_id
        delete_btn_section = html.split('class="delete-btn"')[1].split('</button>')[0]
        assert 'hx-vals=' in delete_btn_section, \
            "Delete button must have hx-vals to pass active_session_id"
        assert 'active_session_id' in delete_btn_section, \
            "hx-vals must include active_session_id"

    def test_empty_sessions_list_renders_correctly(self, jinja_env):
        """
        Verify template handles empty sessions list gracefully.

        When no sessions exist, the template should still render without errors.
        """
        template = jinja_env.get_template("partials/research_sessions_list.html")
        html = template.render(sessions=[], active_session_id=None)

        # Should render sessions-list container
        assert 'class="sessions-list"' in html, \
            "Sessions list container must exist even when empty"

        # Should not have any session-item divs
        assert 'class="session-item' not in html, \
            "Should not have session items when list is empty"

    def test_rename_session_javascript_function_exists(self, jinja_env, sample_sessions):
        """
        Verify the renameSession JavaScript function is defined in the template.

        The function should handle the rename logic with prompt and fetch.
        """
        template = jinja_env.get_template("partials/research_sessions_list.html")
        html = template.render(sessions=sample_sessions, active_session_id="session-1-id")

        # Verify script tag exists
        assert '<script>' in html, "Template must contain script tag"

        # Verify function definition
        assert 'function renameSession(' in html, \
            "renameSession function must be defined"

        # Verify function parameters
        assert 'renameSession(sessionId, currentName)' in html, \
            "renameSession must accept sessionId and currentName parameters"

        # Verify function uses prompt
        assert 'prompt(' in html, \
            "renameSession must use prompt to get new name"

        # Verify function uses fetch for PUT request
        assert 'fetch(' in html, "renameSession must use fetch"
        assert "method: 'PUT'" in html, "renameSession must use PUT method"

    def test_multiple_sessions_each_have_stop_propagation(self, jinja_env):
        """
        Verify that EVERY session's buttons have event.stopPropagation().

        With multiple sessions, each should independently prevent bubbling.
        """
        # Create 5 sessions to test
        many_sessions = [
            {
                "id": f"session-{i}",
                "name": f"Session {i}",
                "folder_path": f"/path/{i}",
                "created_at": datetime.now(timezone.utc).isoformat(),
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }
            for i in range(5)
        ]

        template = jinja_env.get_template("partials/research_sessions_list.html")
        html = template.render(sessions=many_sessions, active_session_id="session-0")

        # Each session should have both event.stopPropagation() instances
        # (one on delete button, one on rename button)
        # Total: 5 sessions * 2 buttons = 10 occurrences
        stop_propagation_count = html.count('event.stopPropagation()')
        assert stop_propagation_count == len(many_sessions) * 2, \
            f"Expected {len(many_sessions) * 2} event.stopPropagation() calls, found {stop_propagation_count}"
