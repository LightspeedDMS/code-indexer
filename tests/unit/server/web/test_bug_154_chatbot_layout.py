"""
Tests for Bug #154: Traditional AI Chatbot Layout - CSS constraints.

Tests verify that the Research Assistant page uses a traditional chatbot layout where:
1. The entire page fits in the viewport (no page scroll)
2. Sessions sidebar scrolls independently
3. Messages container scrolls internally (not the page)
4. Input section is pinned to bottom and always visible

These tests validate CSS structure for proper layout constraints.
"""

import pytest
from pathlib import Path
from bs4 import BeautifulSoup
import re


@pytest.fixture
def template_content():
    """Load research_assistant.html template content."""
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


@pytest.fixture
def style_content(template_content):
    """Extract <style> block content."""
    soup = BeautifulSoup(template_content, 'html.parser')
    styles = soup.find_all('style')
    return '\n'.join([s.string for s in styles if s.string])


class TestChatbotLayoutStructure:
    """Test that HTML structure supports traditional chatbot layout."""

    def test_research_layout_exists(self, template_content):
        """Test: research-layout container must exist."""
        soup = BeautifulSoup(template_content, 'html.parser')
        layout = soup.find(class_='research-layout')
        assert layout is not None, "Must have research-layout container"

    def test_research_sidebar_exists(self, template_content):
        """Test: research-sidebar must exist for sessions."""
        soup = BeautifulSoup(template_content, 'html.parser')
        sidebar = soup.find(class_='research-sidebar')
        assert sidebar is not None, "Must have research-sidebar for sessions list"

    def test_research_chat_area_exists(self, template_content):
        """Test: research-chat-area must exist for chat."""
        soup = BeautifulSoup(template_content, 'html.parser')
        chat_area = soup.find(class_='research-chat-area')
        assert chat_area is not None, "Must have research-chat-area for chat interface"

    def test_chat_messages_exists(self, template_content):
        """Test: chat-messages container must exist."""
        soup = BeautifulSoup(template_content, 'html.parser')
        messages = soup.find(id='chat-messages')
        assert messages is not None, "Must have chat-messages container"

    def test_chat_input_container_exists(self, template_content):
        """Test: chat-input-container form must exist."""
        soup = BeautifulSoup(template_content, 'html.parser')
        input_container = soup.find(class_='chat-input-container')
        assert input_container is not None, "Must have chat-input-container for input box"


class TestLayoutConstraints:
    """Test CSS constraints for no page scroll, independent component scrolling."""

    def test_research_layout_uses_fixed_or_max_height(self, style_content):
        """Test: research-layout must have fixed/max-height to prevent page scroll."""
        # Look for .research-layout CSS rule
        assert '.research-layout' in style_content, "Must have .research-layout CSS"

        # Extract the research-layout rule
        # Match from .research-layout { to the closing }
        pattern = r'\.research-layout\s*{[^}]*}'
        match = re.search(pattern, style_content, re.DOTALL)
        assert match is not None, "Must find .research-layout CSS rule"

        rule = match.group(0)

        # Must have height constraint (height, max-height, or calc with vh units)
        has_height_constraint = (
            'height:' in rule or
            'max-height:' in rule or
            'vh' in rule
        )

        assert has_height_constraint, (
            ".research-layout must have height constraint (height, max-height, or vh units) "
            "to prevent page scrolling"
        )

    def test_research_sidebar_has_overflow_y(self, style_content):
        """Test: research-sidebar must have overflow-y for independent scrolling."""
        assert '.research-sidebar' in style_content, "Must have .research-sidebar CSS"

        # Extract the sidebar rule
        pattern = r'\.research-sidebar\s*{[^}]*}'
        match = re.search(pattern, style_content, re.DOTALL)
        assert match is not None, "Must find .research-sidebar CSS rule"

        rule = match.group(0)

        # Must have overflow (overflow-y: auto or overflow: auto/hidden/scroll)
        has_overflow = (
            'overflow-y' in rule or
            'overflow:' in rule
        )

        assert has_overflow, (
            ".research-sidebar must have overflow-y: auto for independent scrolling "
            "when session list is long"
        )

    def test_chat_messages_has_constrained_height(self, style_content):
        """Test: chat-messages must have max-height to constrain scrolling."""
        assert '.chat-messages' in style_content, "Must have .chat-messages CSS"

        # Extract the chat-messages rule
        pattern = r'\.chat-messages\s*{[^}]*}'
        match = re.search(pattern, style_content, re.DOTALL)
        assert match is not None, "Must find .chat-messages CSS rule"

        rule = match.group(0)

        # Must have max-height or height constraint
        has_height_constraint = (
            'max-height:' in rule or
            'height:' in rule
        )

        assert has_height_constraint, (
            ".chat-messages must have max-height to constrain height and enable internal scrolling"
        )

    def test_chat_messages_has_overflow_y_auto(self, style_content):
        """Test: chat-messages must have overflow-y: auto for scrolling."""
        assert '.chat-messages' in style_content, "Must have .chat-messages CSS"

        # Extract the chat-messages rule
        pattern = r'\.chat-messages\s*{[^}]*}'
        match = re.search(pattern, style_content, re.DOTALL)
        assert match is not None, "Must find .chat-messages CSS rule"

        rule = match.group(0)

        # Must have overflow-y: auto
        assert 'overflow-y' in rule, ".chat-messages must have overflow-y for scrolling"
        assert 'auto' in rule, ".chat-messages overflow-y should be auto"

    def test_chat_area_uses_flexbox_for_pinned_input(self, style_content):
        """Test: research-chat-area must use flexbox to support pinned input."""
        assert '.research-chat-area' in style_content, "Must have .research-chat-area CSS"

        # Extract the chat-area rule
        pattern = r'\.research-chat-area\s*{[^}]*}'
        match = re.search(pattern, style_content, re.DOTALL)
        assert match is not None, "Must find .research-chat-area CSS rule"

        rule = match.group(0)

        # Should use flexbox
        assert 'display:' in rule or 'flex' in rule, (
            ".research-chat-area should use flexbox for layout control"
        )

    def test_chat_container_uses_flexbox_column(self, style_content):
        """Test: chat-container must use flex-direction column for vertical layout."""
        assert '.chat-container' in style_content, "Must have .chat-container CSS"

        # Extract the chat-container rule
        pattern = r'\.chat-container\s*{[^}]*}'
        match = re.search(pattern, style_content, re.DOTALL)
        assert match is not None, "Must find .chat-container CSS rule"

        rule = match.group(0)

        # Should use flexbox with column direction
        has_flex = 'display:' in rule or 'flex' in rule
        has_column = 'flex-direction:' in rule or 'column' in rule

        assert has_flex or has_column, (
            ".chat-container should use flexbox with flex-direction: column "
            "to stack messages and input vertically"
        )


class TestInputPinning:
    """Test that input box is pinned to bottom and always visible."""

    def test_input_container_not_in_scrollable_area(self, template_content):
        """Test: chat-input-container must be outside chat-messages to stay pinned."""
        soup = BeautifulSoup(template_content, 'html.parser')

        chat_messages = soup.find(id='chat-messages')
        input_container = soup.find(class_='chat-input-container')

        assert chat_messages is not None, "Must have chat-messages"
        assert input_container is not None, "Must have chat-input-container"

        # Input container must NOT be a descendant of chat-messages
        # If it were inside chat-messages, it would scroll out of view
        is_descendant = input_container in chat_messages.descendants

        assert not is_descendant, (
            "chat-input-container must NOT be inside chat-messages container. "
            "It must be a sibling to stay pinned to bottom."
        )

    def test_input_container_is_sibling_of_messages(self, template_content):
        """Test: chat-input-container must be a sibling of chat-messages."""
        soup = BeautifulSoup(template_content, 'html.parser')

        chat_container = soup.find(class_='chat-container')
        assert chat_container is not None, "Must have chat-container"

        # Get direct children of chat-container
        # Filter out NavigableString (whitespace) by checking for 'get' method
        children = [
            child for child in chat_container.children
            if hasattr(child, 'get') and (child.get('class') or child.get('id'))
        ]

        # Should have both chat-messages and chat-input-container as children
        has_messages = any(
            child.get('id') == 'chat-messages' or
            'chat-messages' in (child.get('class') or [])
            for child in children
        )

        has_input = any(
            'chat-input-container' in (child.get('class') or [])
            for child in children
        )

        assert has_messages, "chat-container must contain chat-messages"
        assert has_input, "chat-container must contain chat-input-container as sibling to messages"


class TestResponsiveDesign:
    """Test that responsive design maintains layout constraints."""

    def test_responsive_media_query_exists(self, style_content):
        """Test: Must have @media query for mobile responsiveness."""
        assert '@media' in style_content, "Must have @media query for responsive design"

    def test_responsive_maintains_grid_layout(self, style_content):
        """Test: Responsive design must handle grid layout changes."""
        # Look for media query that changes grid-template-columns
        # Should have max-width media query
        pattern = r'@media\s*\([^)]*max-width[^)]*\)\s*{[^}]*grid-template-columns[^}]*}'
        match = re.search(pattern, style_content, re.DOTALL)

        assert match is not None, (
            "Must have @media query that adjusts grid-template-columns for mobile. "
            "Typically changes from 2-column to 1-column layout."
        )
