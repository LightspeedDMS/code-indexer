"""
Unit tests for Bug #154: Research Assistant Smart Auto-Scroll.

Tests verify that chat messages area intelligently auto-scrolls based on user position:
1. If user is at BOTTOM -> auto-scroll ENABLED (new messages scroll into view)
2. If user scrolls UP -> auto-scroll DISABLED (don't interrupt reading)
3. If user scrolls back DOWN to bottom -> auto-scroll RE-ENABLED
4. When user clicks SEND -> always scroll to bottom

Following TDD methodology: Write failing tests FIRST, then implement.
"""

import os
import pytest
from bs4 import BeautifulSoup


class TestResearchAssistantAutoScroll:
    """Test smart auto-scroll behavior for Bug #154."""

    @pytest.fixture
    def research_assistant_page_content(self):
        """Load research_assistant.html template content."""
        # Get path relative to this test file
        test_dir = os.path.dirname(os.path.abspath(__file__))
        template_path = os.path.join(
            test_dir,
            "../../../../src/code_indexer/server/web/templates/research_assistant.html"
        )
        with open(template_path, "r") as f:
            return f.read()

    def _extract_scroll_script(self, page_content):
        """Extract the scroll behavior script from page content."""
        soup = BeautifulSoup(page_content, 'html.parser')
        scripts = soup.find_all('script')

        for script in scripts:
            if script.string and 'scrollToBottom' in script.string and 'htmx:afterSwap' in script.string:
                return script.string

        return None

    def test_auto_scroll_enabled_variable_exists(self, research_assistant_page_content):
        """Test: autoScrollEnabled variable must exist to track scroll state."""
        script = self._extract_scroll_script(research_assistant_page_content)
        assert script is not None, "Must have scroll behavior script"
        assert 'autoScrollEnabled' in script, (
            "Must have autoScrollEnabled variable to track whether to auto-scroll"
        )

    def test_auto_scroll_enabled_defaults_to_true(self, research_assistant_page_content):
        """Test: autoScrollEnabled must default to true (user starts at bottom)."""
        script = self._extract_scroll_script(research_assistant_page_content)
        assert script is not None, "Must have scroll behavior script"

        # Check that autoScrollEnabled is initialized to true
        assert 'autoScrollEnabled = true' in script or 'autoScrollEnabled=true' in script, (
            "autoScrollEnabled must default to true so initial messages scroll into view"
        )

    def test_is_at_bottom_function_exists(self, research_assistant_page_content):
        """Test: isAtBottom() function must exist to check scroll position."""
        script = self._extract_scroll_script(research_assistant_page_content)
        assert script is not None, "Must have scroll behavior script"
        assert 'isAtBottom' in script, (
            "Must have isAtBottom() function to check if user is at bottom of chat"
        )

    def test_scroll_threshold_exists(self, research_assistant_page_content):
        """Test: SCROLL_THRESHOLD constant must exist for bottom detection."""
        script = self._extract_scroll_script(research_assistant_page_content)
        assert script is not None, "Must have scroll behavior script"
        assert 'SCROLL_THRESHOLD' in script or 'threshold' in script, (
            "Must have scroll threshold constant for bottom detection tolerance"
        )

    def test_scroll_event_listener_exists(self, research_assistant_page_content):
        """Test: scroll event listener must exist on chatMessages element."""
        script = self._extract_scroll_script(research_assistant_page_content)
        assert script is not None, "Must have scroll behavior script"
        assert "'scroll'" in script or '"scroll"' in script, (
            "Must have scroll event listener to update autoScrollEnabled state"
        )

    def test_scroll_listener_updates_auto_scroll_enabled(self, research_assistant_page_content):
        """Test: scroll event listener must update autoScrollEnabled based on position."""
        script = self._extract_scroll_script(research_assistant_page_content)
        assert script is not None, "Must have scroll behavior script"

        # The scroll listener should update autoScrollEnabled
        assert "'scroll'" in script or '"scroll"' in script, "Must have scroll event listener"

        # Look for the pattern where autoScrollEnabled is updated in scroll handler
        lines = script.split('\n')
        found_scroll_listener = False
        found_update = False

        for i, line in enumerate(lines):
            if "'scroll'" in line or '"scroll"' in line:
                found_scroll_listener = True
                # Check next 5 lines for autoScrollEnabled update
                for j in range(i, min(i+5, len(lines))):
                    if 'autoScrollEnabled' in lines[j] and 'isAtBottom' in lines[j]:
                        found_update = True
                        break

        assert found_scroll_listener, "Must have scroll event listener"
        assert found_update, (
            "Scroll event listener must update autoScrollEnabled based on isAtBottom()"
        )

    def test_afterswap_checks_auto_scroll_enabled(self, research_assistant_page_content):
        """Test: htmx:afterSwap must check autoScrollEnabled before scrolling."""
        script = self._extract_scroll_script(research_assistant_page_content)
        assert script is not None, "Must have scroll behavior script"

        # Must have afterSwap handler
        assert 'htmx:afterSwap' in script, "Must have htmx:afterSwap event listener"

        # Parse afterSwap handler
        lines = script.split('\n')
        in_afterswap = False
        afterswap_code = []
        brace_depth = 0

        for line in lines:
            if 'htmx:afterSwap' in line:
                in_afterswap = True

            if in_afterswap:
                afterswap_code.append(line)
                brace_depth += line.count('{') - line.count('}')
                if brace_depth == 0 and len(afterswap_code) > 1:
                    break

        afterswap_text = '\n'.join(afterswap_code)

        # The afterSwap handler must check autoScrollEnabled before scrolling
        assert 'autoScrollEnabled' in afterswap_text, (
            "afterSwap handler must check autoScrollEnabled state before scrolling"
        )
        assert 'scrollToBottom' in afterswap_text, (
            "afterSwap handler must call scrollToBottom() when autoScrollEnabled is true"
        )

    def test_send_form_always_scrolls(self, research_assistant_page_content):
        """Test: Send form submission must ALWAYS scroll to bottom and re-enable auto-scroll."""
        script = self._extract_scroll_script(research_assistant_page_content)
        assert script is not None, "Must have scroll behavior script"

        # Must have beforeRequest handler for send form
        assert 'htmx:beforeRequest' in script or 'beforeRequest' in script, (
            "Must have htmx:beforeRequest handler to handle send form submission"
        )

        # Parse to find the send form handler
        lines = script.split('\n')
        found_send_handler = False

        for i, line in enumerate(lines):
            if ('beforeRequest' in line or 'htmx:beforeRequest' in line):
                # Check next 10 lines for send form logic
                context = '\n'.join(lines[i:min(i+10, len(lines))])
                if '/admin/research/send' in context or 'send' in context.lower():
                    found_send_handler = True
                    # Must set autoScrollEnabled to true
                    assert 'autoScrollEnabled = true' in context or 'autoScrollEnabled=true' in context, (
                        "Send form handler must set autoScrollEnabled = true"
                    )
                    # Must call scrollToBottom
                    assert 'scrollToBottom' in context, (
                        "Send form handler must call scrollToBottom()"
                    )
                    break

        assert found_send_handler, (
            "Must have htmx:beforeRequest handler that detects send form and scrolls"
        )

    def test_scrolltop_and_scrollheight_used_for_scrolling(
        self, research_assistant_page_content
    ):
        """Test: Scroll implementation must use scrollTop = scrollHeight."""
        assert 'scrollTop' in research_assistant_page_content, (
            "Must use scrollTop for scrolling"
        )
        assert 'scrollHeight' in research_assistant_page_content, (
            "Must use scrollHeight to scroll to bottom"
        )

    def test_chat_messages_container_has_overflow_y(
        self, research_assistant_page_content
    ):
        """Test: chat-messages container must have overflow-y: auto for scrolling."""
        soup = BeautifulSoup(research_assistant_page_content, 'html.parser')

        # Find the style block
        styles = soup.find_all('style')
        style_content = '\n'.join([s.string for s in styles if s.string])

        # Check for .chat-messages class with overflow-y
        assert '.chat-messages' in style_content, (
            "Must have .chat-messages CSS class"
        )
        assert 'overflow-y' in style_content, (
            "chat-messages must have overflow-y for scrolling"
        )

    def test_chat_messages_element_exists_in_html(
        self, research_assistant_page_content
    ):
        """Test: chat-messages element must exist with correct id."""
        soup = BeautifulSoup(research_assistant_page_content, 'html.parser')

        # Find chat-messages div
        chat_messages = soup.find(id='chat-messages')
        assert chat_messages is not None, (
            "Must have element with id='chat-messages'"
        )

        # Verify it's the target for HTMX swaps
        assert 'class' in chat_messages.attrs, (
            "chat-messages must have class attribute"
        )
        assert 'chat-messages' in chat_messages.get('class', []), (
            "Element must have chat-messages class"
        )

    def test_initial_scroll_to_bottom_on_load(self, research_assistant_page_content):
        """Test: Page must scroll to bottom on initial load."""
        script = self._extract_scroll_script(research_assistant_page_content)
        assert script is not None, "Must have scroll behavior script"

        # Must call scrollToBottom() outside event handlers (for initial load)
        lines = script.split('\n')
        found_initial_scroll = False

        # Look for scrollToBottom() that's not inside an event listener
        in_event_handler = False
        brace_depth = 0

        for line in lines:
            if 'addEventListener' in line or 'htmx:' in line:
                in_event_handler = True

            if in_event_handler:
                brace_depth += line.count('{') - line.count('}')
                if brace_depth == 0:
                    in_event_handler = False

            if not in_event_handler and 'scrollToBottom()' in line:
                found_initial_scroll = True
                break

        assert found_initial_scroll, (
            "Must call scrollToBottom() on page load (outside event handlers)"
        )
