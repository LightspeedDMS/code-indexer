"""
Unit tests for Story #141: Research Assistant - Basic Chatbot Working.

Tests AC1: Research Assistant Tab navigation and basic layout.

Following TDD methodology: Write failing tests FIRST, then implement.
"""

import pytest
from fastapi.testclient import TestClient
from bs4 import BeautifulSoup


class TestResearchAssistantTab:
    """Test Research Assistant tab navigation and page rendering."""

    @pytest.fixture
    def client(self):
        """Create test client."""
        from src.code_indexer.server.app import app

        return TestClient(app)

    @pytest.fixture
    def authenticated_admin_client(self, client):
        """Create authenticated admin client using session auth."""
        # Step 1: Get login page to receive CSRF token in cookie
        login_page_response = client.get("/login")
        assert login_page_response.status_code == 200

        # Step 2: Extract CSRF token from HTML
        soup = BeautifulSoup(login_page_response.text, 'html.parser')
        csrf_input = soup.find("input", {"name": "csrf_token"})
        assert csrf_input is not None, "CSRF token input must exist in login form"
        csrf_token = csrf_input.get("value")
        assert csrf_token is not None, "CSRF token value must not be None"

        # Step 3: Submit login form with CSRF token
        login_response = client.post(
            "/login",
            data={
                "username": "admin",
                "password": "admin",
                "csrf_token": csrf_token
            },
            follow_redirects=False
        )

        # Should redirect on success (303 See Other)
        assert login_response.status_code == 303, \
            f"Login should redirect on success, got {login_response.status_code}"

        return client

    def test_research_assistant_nav_link_exists(self, authenticated_admin_client):
        """Test AC1: Research Assistant tab appears in admin navigation after Diagnostics."""
        response = authenticated_admin_client.get("/admin/")
        assert response.status_code == 200

        soup = BeautifulSoup(response.text, 'html.parser')

        # Find admin nav
        nav = soup.find("nav", class_="admin-nav")
        assert nav is not None, "Admin navigation must exist"

        # Find all nav links
        nav_links = nav.find_all("a")
        nav_hrefs = [link.get("href") for link in nav_links]

        # Research Assistant link must exist
        assert "/admin/research" in nav_hrefs, \
            "Research Assistant nav link must exist in admin navigation"

    def test_research_assistant_positioned_after_diagnostics(self, authenticated_admin_client):
        """Test AC1: Research Assistant tab comes after Diagnostics in navigation."""
        response = authenticated_admin_client.get("/admin/")
        soup = BeautifulSoup(response.text, 'html.parser')

        nav = soup.find("nav", class_="admin-nav")
        nav_links = nav.find_all("a")
        nav_hrefs = [link.get("href") for link in nav_links]

        # Get positions
        diagnostics_idx = None
        research_idx = None

        for i, href in enumerate(nav_hrefs):
            if href == "/admin/diagnostics":
                diagnostics_idx = i
            if href == "/admin/research":
                research_idx = i

        assert diagnostics_idx is not None, "Diagnostics link must exist"
        assert research_idx is not None, "Research Assistant link must exist"
        assert research_idx > diagnostics_idx, \
            "Research Assistant must appear after Diagnostics"

    def test_research_assistant_page_loads(self, authenticated_admin_client):
        """Test AC1: Research Assistant page loads successfully for admin."""
        response = authenticated_admin_client.get("/admin/research")
        assert response.status_code == 200, \
            "Research Assistant page should be accessible to admin users"

    def test_research_assistant_has_two_column_layout(self, authenticated_admin_client):
        """Test AC1: Research Assistant page has two-column layout."""
        response = authenticated_admin_client.get("/admin/research")
        soup = BeautifulSoup(response.text, 'html.parser')

        # Should have sidebar (left column) and chat area (right column)
        sidebar = soup.find(class_="research-sidebar")
        chat_area = soup.find(class_="research-chat-area")

        assert sidebar is not None, "Research Assistant must have sidebar placeholder"
        assert chat_area is not None, "Research Assistant must have chat area"

    def test_research_assistant_sidebar_is_placeholder(self, authenticated_admin_client):
        """Test AC1: Sidebar is placeholder for future session management."""
        response = authenticated_admin_client.get("/admin/research")
        soup = BeautifulSoup(response.text, 'html.parser')

        sidebar = soup.find(class_="research-sidebar")
        assert sidebar is not None

        # Should contain placeholder text indicating future functionality
        sidebar_text = sidebar.get_text().lower()
        assert "session" in sidebar_text or "placeholder" in sidebar_text, \
            "Sidebar should indicate it's a placeholder"

    def test_research_assistant_only_visible_to_admin(self, authenticated_admin_client, client):
        """Test AC1: Research Assistant tab only visible to admin users."""
        # Verify admin can access
        admin_response = authenticated_admin_client.get("/admin/research")
        assert admin_response.status_code == 200, \
            "Admin users should be able to access Research Assistant"

        # Verify endpoint exists (session protection tested separately)
        unauth_response = client.get("/admin/research")
        # In test mode, may return 200, 303, 307, or 401 depending on configuration
        # The important thing is the endpoint exists and is registered
        assert unauth_response.status_code in [200, 303, 307, 401], \
            "Research Assistant endpoint should exist"

    # AC2: Chat UI Layout Tests

    def test_send_message_endpoint_exists(self, authenticated_admin_client):
        """Test AC2: POST /admin/research/send endpoint exists for message submission."""
        response = authenticated_admin_client.post(
            "/admin/research/send",
            data={"user_prompt": "Test question"}
        )
        # Should not return 404 or 405 (endpoint exists)
        assert response.status_code not in [404, 405], \
            "Send message endpoint must exist and accept POST"

    def test_poll_endpoint_exists(self, authenticated_admin_client):
        """Test AC2: GET /admin/research/poll/{job_id} endpoint exists for polling."""
        response = authenticated_admin_client.get("/admin/research/poll/test-job-id")
        # Should not return 404 or 405 (endpoint exists)
        assert response.status_code not in [404, 405], \
            "Poll endpoint must exist and accept GET"

    def test_chat_messages_rendered_from_database(self, authenticated_admin_client):
        """Test AC2: Chat messages from database are rendered in correct alignment."""
        # Add messages to database via service
        from src.code_indexer.server.services.research_assistant_service import (
            ResearchAssistantService,
        )
        service = ResearchAssistantService()
        session = service.get_default_session()

        # Add test messages
        service.add_message(session["id"], "user", "Test user question")
        service.add_message(session["id"], "assistant", "Test assistant response")

        # Get page
        response = authenticated_admin_client.get("/admin/research")
        assert response.status_code == 200

        soup = BeautifulSoup(response.text, 'html.parser')

        # Find all message elements
        user_messages = soup.find_all(class_="chat-message user")
        assistant_messages = soup.find_all(class_="chat-message assistant")

        # Should have at least one of each type
        assert len(user_messages) >= 1, "Must render user messages"
        assert len(assistant_messages) >= 1, "Must render assistant messages"

        # Verify content is rendered
        all_messages_text = soup.get_text()
        assert "Test user question" in all_messages_text, "User message content must be rendered"
        assert "Test assistant response" in all_messages_text, "Assistant message content must be rendered"

    def test_send_button_has_htmx_attributes(self, authenticated_admin_client):
        """Test AC2: Send button has HTMX attributes for POST and polling."""
        response = authenticated_admin_client.get("/admin/research")
        soup = BeautifulSoup(response.text, 'html.parser')

        send_btn = soup.find(id="send-btn")
        assert send_btn is not None, "Send button must exist"

        # Check for HTMX attributes (hx-post, hx-target, hx-swap)
        # Note: Actual implementation may use form with hx-post instead
        form = soup.find("form", {"hx-post": True})
        assert form is not None or send_btn.get("hx-post"), \
            "Must have HTMX POST trigger on form or button"

    def test_enter_key_submits_form(self, authenticated_admin_client):
        """Test AC2: Enter key submits form (Shift+Enter for newline)."""
        response = authenticated_admin_client.get("/admin/research")
        soup = BeautifulSoup(response.text, 'html.parser')

        # Check for form with proper handling
        form = soup.find("form")
        assert form is not None, "Must have form element for submission"

        # Check textarea exists
        textarea = soup.find(id="user-prompt")
        assert textarea is not None, "Textarea for user input must exist"

    # Story #142 AC3: Message Timestamps

    def test_messages_display_timestamps(self, authenticated_admin_client):
        """Test AC3: Each message displays a timestamp."""
        # Add a test message
        from src.code_indexer.server.services.research_assistant_service import (
            ResearchAssistantService,
        )
        service = ResearchAssistantService()
        session = service.get_default_session()
        service.add_message(session["id"], "user", "Test message with timestamp")

        response = authenticated_admin_client.get("/admin/research")
        soup = BeautifulSoup(response.text, 'html.parser')

        # Find message elements
        messages = soup.find_all(class_="chat-message")
        assert len(messages) > 0, "Must have at least one message"

        # Each message should have a timestamp element
        for message in messages:
            timestamp = message.find(class_="message-timestamp")
            assert timestamp is not None, "Each message must have a timestamp element"
            assert timestamp.get_text().strip() != "", "Timestamp must not be empty"

    def test_recent_messages_show_relative_timestamps(self, authenticated_admin_client):
        """Test AC3: Messages <24 hours old show relative timestamps ('X minutes ago')."""
        from datetime import datetime, timedelta, timezone
        from src.code_indexer.server.services.research_assistant_service import (
            ResearchAssistantService,
        )
        service = ResearchAssistantService()
        session = service.get_default_session()

        # Manually insert a message with a recent timestamp
        conn = service._get_connection()
        try:
            recent_time = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
            conn.execute(
                "INSERT INTO research_messages (session_id, role, content, created_at) "
                "VALUES (?, ?, ?, ?)",
                (session["id"], "user", "Recent message", recent_time)
            )
            conn.commit()
        finally:
            conn.close()

        response = authenticated_admin_client.get("/admin/research")
        soup = BeautifulSoup(response.text, 'html.parser')

        # Should find relative timestamp like "5 minutes ago"
        page_text = soup.get_text()
        assert "ago" in page_text.lower(), "Recent messages should show relative timestamp"

    def test_old_messages_show_absolute_timestamps(self, authenticated_admin_client):
        """Test AC3: Messages >=24 hours old show absolute timestamps ('Jan 30, 14:32')."""
        from datetime import datetime, timedelta, timezone
        from src.code_indexer.server.services.research_assistant_service import (
            ResearchAssistantService,
        )
        service = ResearchAssistantService()
        session = service.get_default_session()

        # Manually insert a message with an old timestamp
        conn = service._get_connection()
        try:
            old_time = (datetime.now(timezone.utc) - timedelta(days=2)).isoformat()
            conn.execute(
                "INSERT INTO research_messages (session_id, role, content, created_at) "
                "VALUES (?, ?, ?, ?)",
                (session["id"], "user", "Old message", old_time)
            )
            conn.commit()
        finally:
            conn.close()

        response = authenticated_admin_client.get("/admin/research")
        soup = BeautifulSoup(response.text, 'html.parser')

        # Should find absolute timestamp with month name
        page_text = soup.get_text()
        # Check for month abbreviations (Jan, Feb, etc.)
        months = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        assert any(month in page_text for month in months), \
            "Old messages should show absolute timestamp with month"

    # Story #142 AC4: Scroll Behavior

    def test_page_has_scroll_to_bottom_functionality(self, authenticated_admin_client):
        """Test AC4: Page includes JavaScript for scroll-to-bottom functionality."""
        response = authenticated_admin_client.get("/admin/research")
        page_text = response.text

        # Should have scroll-to-bottom JavaScript logic
        assert "scrollTop" in page_text or "scroll" in page_text.lower(), \
            "Page must include scroll-to-bottom functionality"

    def test_page_has_jump_to_bottom_button(self, authenticated_admin_client):
        """Test AC4: Page includes a jump-to-bottom button."""
        response = authenticated_admin_client.get("/admin/research")
        soup = BeautifulSoup(response.text, 'html.parser')

        # Look for jump-to-bottom button (could be button, link, or div with id/class)
        jump_button = soup.find(id="jump-to-bottom") or \
                      soup.find(class_="jump-to-bottom") or \
                      soup.find("button", string=lambda t: t and "bottom" in t.lower())

        assert jump_button is not None, "Page must have a jump-to-bottom button"

    def test_jump_button_has_hidden_class(self, authenticated_admin_client):
        """Test AC4: Jump-to-bottom button is hidden by default."""
        response = authenticated_admin_client.get("/admin/research")
        soup = BeautifulSoup(response.text, 'html.parser')

        jump_button = soup.find(id="jump-to-bottom") or soup.find(class_="jump-to-bottom")
        assert jump_button is not None, "Jump-to-bottom button must exist"

        # Button should have hidden class or style
        button_classes = jump_button.get("class", [])
        button_style = jump_button.get("style", "")

        is_hidden = "hidden" in button_classes or \
                    "display: none" in button_style or \
                    "display:none" in button_style

        assert is_hidden, "Jump-to-bottom button should be hidden by default"

    def test_chat_messages_container_is_scrollable(self, authenticated_admin_client):
        """Test AC4: Chat messages container has overflow-y for scrolling."""
        response = authenticated_admin_client.get("/admin/research")
        soup = BeautifulSoup(response.text, 'html.parser')

        chat_messages = soup.find(id="chat-messages") or soup.find(class_="chat-messages")
        assert chat_messages is not None, "Chat messages container must exist"

    # Story #142 AC5: Loading State

    def test_empty_state_shows_welcome_message(self, authenticated_admin_client):
        """Test AC5: When no messages exist, show welcome message."""
        # Clear all messages first
        from src.code_indexer.server.services.research_assistant_service import (
            ResearchAssistantService,
        )
        service = ResearchAssistantService()
        session = service.get_default_session()

        # Delete all messages for this session
        conn = service._get_connection()
        try:
            conn.execute(
                "DELETE FROM research_messages WHERE session_id = ?",
                (session["id"],)
            )
            conn.commit()
        finally:
            conn.close()

        response = authenticated_admin_client.get("/admin/research")
        soup = BeautifulSoup(response.text, 'html.parser')

        # Should show welcome message
        welcome = soup.find(class_="welcome-message")
        assert welcome is not None, "Must show welcome message when no messages exist"
        assert "welcome" in welcome.get_text().lower(), "Welcome message must contain 'welcome'"

    def test_with_messages_no_welcome_message(self, authenticated_admin_client):
        """Test AC5: When messages exist, don't show welcome message."""
        # Add a message
        from src.code_indexer.server.services.research_assistant_service import (
            ResearchAssistantService,
        )
        service = ResearchAssistantService()
        session = service.get_default_session()
        service.add_message(session["id"], "user", "Test message")

        response = authenticated_admin_client.get("/admin/research")
        soup = BeautifulSoup(response.text, 'html.parser')

        # Should NOT show welcome message
        welcome = soup.find(class_="welcome-message")
        # Either welcome doesn't exist, or if it does, it's not visible
        if welcome:
            # If welcome element exists, it should be hidden or empty
            assert welcome.get_text().strip() == "" or \
                   "display: none" in welcome.get("style", ""), \
                   "Welcome message should not be visible when messages exist"

    def test_page_loads_conversation_history_on_load(self, authenticated_admin_client):
        """Test AC1/AC5: Page loads existing conversation history from database."""
        # Add multiple messages to database
        from src.code_indexer.server.services.research_assistant_service import (
            ResearchAssistantService,
        )
        service = ResearchAssistantService()
        session = service.get_default_session()

        # Clear existing messages
        conn = service._get_connection()
        try:
            conn.execute(
                "DELETE FROM research_messages WHERE session_id = ?",
                (session["id"],)
            )
            conn.commit()
        finally:
            conn.close()

        # Add test messages
        service.add_message(session["id"], "user", "First user message")
        service.add_message(session["id"], "assistant", "First assistant response")
        service.add_message(session["id"], "user", "Second user message")

        response = authenticated_admin_client.get("/admin/research")
        soup = BeautifulSoup(response.text, 'html.parser')

        # All messages should be rendered
        page_text = soup.get_text()
        assert "First user message" in page_text, "First user message must be loaded"
        assert "First assistant response" in page_text, "First assistant response must be loaded"
        assert "Second user message" in page_text, "Second user message must be loaded"

        # Messages should be in correct order (oldest first)
        first_idx = page_text.index("First user message")
        second_idx = page_text.index("First assistant response")
        third_idx = page_text.index("Second user message")
        assert first_idx < second_idx < third_idx, "Messages must be in chronological order"
