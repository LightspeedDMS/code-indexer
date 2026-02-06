"""
Unit tests for Bug #152: Research Assistant Panel Dies After Session Rename.

Problem:
After renaming a session, clicking on session labels does nothing because
HTMX attributes on dynamically inserted HTML aren't initialized.

Root Cause:
In renameSession() function, after innerHTML replacement, HTMX doesn't
automatically process the new HTML. The new elements have hx-get, hx-on::before-request
attributes, but HTMX hasn't initialized them.

Solution:
Call htmx.process() after innerHTML replacement to re-initialize HTMX on the new content.

Test Strategy:
Since this is JavaScript in a Jinja2 template, we verify:
1. The renameSession function exists
2. The function contains htmx.process() call after innerHTML assignment
3. The call targets the correct element (#sessions-sidebar)

Acceptance Criteria:
- AC1: renameSession function contains htmx.process() call
- AC2: htmx.process() is called AFTER innerHTML assignment
- AC3: htmx.process() targets document.getElementById('sessions-sidebar')
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


class TestResearchSessionsHTMXReinitialization:
    """Test HTMX reinitialization after dynamic HTML insertion (Bug #152)."""

    def test_rename_session_function_calls_htmx_process(self, jinja_env, sample_sessions):
        """
        AC1: renameSession function contains htmx.process() call.

        Verifies that the renameSession function includes a call to htmx.process()
        to reinitialize HTMX attributes on dynamically inserted HTML.
        """
        template = jinja_env.get_template("partials/research_sessions_list.html")
        html = template.render(sessions=sample_sessions, active_session_id="session-1-id")

        # Verify renameSession function exists
        assert 'function renameSession(' in html, "renameSession function must exist"

        # Verify htmx.process() is called
        assert 'htmx.process(' in html, \
            "renameSession must call htmx.process() to reinitialize HTMX on dynamic HTML"

    def test_htmx_process_called_after_innerhtml(self, jinja_env, sample_sessions):
        """
        AC2: htmx.process() is called AFTER innerHTML assignment.

        Verifies that htmx.process() comes after the innerHTML replacement,
        ensuring HTMX processes the newly inserted content.
        """
        template = jinja_env.get_template("partials/research_sessions_list.html")
        html = template.render(sessions=sample_sessions, active_session_id="session-1-id")

        # Extract the renameSession function body
        script_start = html.index('<script>')
        script_end = html.index('</script>', script_start)
        script_content = html[script_start:script_end]

        # Verify innerHTML assignment exists
        assert 'innerHTML = html' in script_content, \
            "renameSession must use innerHTML to update content"

        # Verify htmx.process() exists
        assert 'htmx.process(' in script_content, \
            "renameSession must call htmx.process()"

        # Verify order: innerHTML comes BEFORE htmx.process()
        innerhtml_idx = script_content.index('innerHTML = html')
        htmx_process_idx = script_content.index('htmx.process(')
        assert innerhtml_idx < htmx_process_idx, \
            "htmx.process() must be called AFTER innerHTML assignment"

    def test_htmx_process_targets_correct_element(self, jinja_env, sample_sessions):
        """
        AC3: htmx.process() targets document.getElementById('sessions-sidebar').

        Verifies that htmx.process() is called on the correct element that
        contains the dynamically updated HTML.
        """
        template = jinja_env.get_template("partials/research_sessions_list.html")
        html = template.render(sessions=sample_sessions, active_session_id="session-1-id")

        # Extract script content
        script_start = html.index('<script>')
        script_end = html.index('</script>', script_start)
        script_content = html[script_start:script_end]

        # Verify htmx.process() is called with getElementById('sessions-sidebar')
        assert "htmx.process(document.getElementById('sessions-sidebar'))" in script_content, \
            "htmx.process() must target document.getElementById('sessions-sidebar')"

    def test_complete_fix_pattern(self, jinja_env, sample_sessions):
        """
        Integration test: Verify the complete fix pattern is present.

        The fix should follow this pattern:
        1. fetch() with PUT method
        2. .then(response => response.text())
        3. .then(html => {
        4.     document.getElementById('sessions-sidebar').innerHTML = html;
        5.     htmx.process(document.getElementById('sessions-sidebar'));
        6. });
        """
        template = jinja_env.get_template("partials/research_sessions_list.html")
        html = template.render(sessions=sample_sessions, active_session_id="session-1-id")

        # Extract script content
        script_start = html.index('<script>')
        script_end = html.index('</script>', script_start)
        script_content = html[script_start:script_end]

        # Verify the complete fix pattern
        required_components = [
            "fetch('/admin/research/sessions/' + sessionId",  # Fetch call
            "method: 'PUT'",                                   # PUT method
            ".then(response => response.text())",             # Parse response
            ".then(html => {",                                 # HTML handler
            "document.getElementById('sessions-sidebar').innerHTML = html;",  # Update HTML
            "htmx.process(document.getElementById('sessions-sidebar'));",     # Reinitialize HTMX
        ]

        for component in required_components:
            assert component in script_content, \
                f"Missing required component in renameSession fix: {component}"

    def test_no_regression_in_existing_functionality(self, jinja_env, sample_sessions):
        """
        AC4: All existing tests pass (no regressions).

        Verifies that adding htmx.process() doesn't break existing functionality:
        - prompt() for new name
        - FormData creation
        - fetch() with PUT
        - innerHTML update
        """
        template = jinja_env.get_template("partials/research_sessions_list.html")
        html = template.render(sessions=sample_sessions, active_session_id="session-1-id")

        # Extract script content
        script_start = html.index('<script>')
        script_end = html.index('</script>', script_start)
        script_content = html[script_start:script_end]

        # Verify all original functionality is preserved
        assert 'var newName = prompt(' in script_content, \
            "Must still prompt user for new name"
        assert 'var formData = new FormData()' in script_content, \
            "Must still create FormData"
        assert "formData.append('new_name', newName)" in script_content, \
            "Must still append new_name to FormData"
        assert "method: 'PUT'" in script_content, \
            "Must still use PUT method"
        assert 'fetch(' in script_content, \
            "Must still use fetch API"

    def test_htmx_process_only_in_rename_success_path(self, jinja_env, sample_sessions):
        """
        Verify htmx.process() is only called in the success path.

        The function should only call htmx.process() when:
        1. User enters a new name (not cancelled)
        2. New name is different from current name
        3. Fetch succeeds and returns HTML

        This test verifies the logical structure is preserved.
        """
        template = jinja_env.get_template("partials/research_sessions_list.html")
        html = template.render(sessions=sample_sessions, active_session_id="session-1-id")

        # Extract script content
        script_start = html.index('<script>')
        script_end = html.index('</script>', script_start)
        script_content = html[script_start:script_end]

        # Verify htmx.process() is inside the success callback
        # Should be inside .then(html => { ... })
        then_html_start = script_content.index('.then(html => {')
        then_html_section = script_content[then_html_start:]

        # Find the closing brace of the .then callback
        # Count braces to find the matching close
        brace_count = 0
        then_html_end = 0
        for i, char in enumerate(then_html_section):
            if char == '{':
                brace_count += 1
            elif char == '}':
                brace_count -= 1
                if brace_count == 0:
                    then_html_end = i
                    break

        then_html_callback = then_html_section[:then_html_end + 1]

        # Verify htmx.process() is inside this callback
        assert 'htmx.process(' in then_html_callback, \
            "htmx.process() must be inside the .then(html => {...}) success callback"

        # Verify innerHTML is also in the same callback
        assert 'innerHTML = html' in then_html_callback, \
            "innerHTML update must be in the same callback as htmx.process()"
