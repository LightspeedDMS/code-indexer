"""
Unit tests for Story #141: Research Assistant Service - SQLite Storage.

Tests AC6: SQLite Storage for research sessions and messages.

Following TDD methodology: Write failing tests FIRST, then implement.
"""

import pytest
import sqlite3
import tempfile
from pathlib import Path
from datetime import datetime


class TestResearchAssistantStorage:
    """Test Research Assistant SQLite storage."""

    @pytest.fixture
    def temp_db(self):
        """Create temporary database for testing."""
        import os
        # Create temp dir and database path
        temp_dir = tempfile.mkdtemp()
        db_path = os.path.join(temp_dir, "test.db")
        yield db_path
        # Cleanup
        Path(db_path).unlink(missing_ok=True)
        Path(temp_dir).rmdir()

    @pytest.fixture
    def research_service(self, temp_db):
        """Create ResearchAssistantService with temporary database."""
        from src.code_indexer.server.storage.database_manager import DatabaseSchema
        from src.code_indexer.server.services.research_assistant_service import (
            ResearchAssistantService,
        )

        # Initialize database schema first
        schema = DatabaseSchema(db_path=temp_db)
        schema.initialize_database()

        return ResearchAssistantService(db_path=temp_db)

    def test_research_sessions_table_created(self, temp_db):
        """Test AC6: research_sessions table is created with correct schema."""
        from src.code_indexer.server.storage.database_manager import DatabaseSchema

        schema = DatabaseSchema(db_path=temp_db)
        schema.initialize_database()

        conn = sqlite3.connect(temp_db)
        try:
            # Check table exists
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='research_sessions'"
            )
            assert cursor.fetchone() is not None, "research_sessions table must exist"

            # Check schema columns
            cursor = conn.execute("PRAGMA table_info(research_sessions)")
            columns = {row[1]: row[2] for row in cursor.fetchall()}

            assert "id" in columns, "Must have id column"
            assert "name" in columns, "Must have name column"
            assert "folder_path" in columns, "Must have folder_path column"
            assert "created_at" in columns, "Must have created_at column"
            assert "updated_at" in columns, "Must have updated_at column"

            assert columns["id"] == "TEXT", "id must be TEXT"
            assert columns["name"] == "TEXT", "name must be TEXT"
            assert columns["folder_path"] == "TEXT", "folder_path must be TEXT"
        finally:
            conn.close()

    def test_research_messages_table_created(self, temp_db):
        """Test AC6: research_messages table is created with correct schema."""
        from src.code_indexer.server.storage.database_manager import DatabaseSchema

        schema = DatabaseSchema(db_path=temp_db)
        schema.initialize_database()

        conn = sqlite3.connect(temp_db)
        try:
            # Check table exists
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='research_messages'"
            )
            assert cursor.fetchone() is not None, "research_messages table must exist"

            # Check schema columns
            cursor = conn.execute("PRAGMA table_info(research_messages)")
            columns = {row[1]: row[2] for row in cursor.fetchall()}

            assert "id" in columns, "Must have id column"
            assert "session_id" in columns, "Must have session_id column"
            assert "role" in columns, "Must have role column"
            assert "content" in columns, "Must have content column"
            assert "created_at" in columns, "Must have created_at column"

            assert columns["id"] == "INTEGER", "id must be INTEGER"
            assert columns["session_id"] == "TEXT", "session_id must be TEXT"
            assert columns["role"] == "TEXT", "role must be TEXT"
            assert columns["content"] == "TEXT", "content must be TEXT"
        finally:
            conn.close()

    def test_default_session_auto_created(self, research_service):
        """Test AC6: Default session is auto-created on first access."""
        session = research_service.get_default_session()

        assert session is not None, "Default session must be auto-created"
        assert session["id"] == "default", "Default session must have id='default'"
        assert session["name"] == "Default Session", "Must have correct name"
        assert "folder_path" in session, "Must have folder_path"
        assert "created_at" in session, "Must have created_at"
        assert "updated_at" in session, "Must have updated_at"

    def test_store_user_message(self, research_service):
        """Test AC6: Can store user messages."""
        session = research_service.get_default_session()
        session_id = session["id"]

        message = research_service.add_message(
            session_id=session_id, role="user", content="Test question"
        )

        assert message is not None, "Message must be stored"
        assert message["role"] == "user", "Role must be 'user'"
        assert message["content"] == "Test question", "Content must match"
        assert "created_at" in message, "Must have created_at timestamp"
        assert message["id"] > 0, "Message ID must be positive integer"

    def test_store_assistant_message(self, research_service):
        """Test AC6: Can store assistant messages."""
        session = research_service.get_default_session()
        session_id = session["id"]

        message = research_service.add_message(
            session_id=session_id, role="assistant", content="Test response"
        )

        assert message is not None, "Message must be stored"
        assert message["role"] == "assistant", "Role must be 'assistant'"
        assert message["content"] == "Test response", "Content must match"

    def test_retrieve_messages_for_session(self, research_service):
        """Test AC6: Can retrieve all messages for a session in order."""
        session = research_service.get_default_session()
        session_id = session["id"]

        # Add multiple messages
        research_service.add_message(session_id, "user", "Question 1")
        research_service.add_message(session_id, "assistant", "Answer 1")
        research_service.add_message(session_id, "user", "Question 2")
        research_service.add_message(session_id, "assistant", "Answer 2")

        # Retrieve messages
        messages = research_service.get_messages(session_id)

        assert len(messages) == 4, "Must retrieve all 4 messages"
        assert messages[0]["role"] == "user", "First message must be user"
        assert messages[0]["content"] == "Question 1", "First message content"
        assert messages[1]["role"] == "assistant", "Second message must be assistant"
        assert messages[2]["role"] == "user", "Third message must be user"
        assert messages[3]["role"] == "assistant", "Fourth message must be assistant"

    def test_messages_ordered_by_creation_time(self, research_service):
        """Test AC6: Messages are returned in creation order."""
        session = research_service.get_default_session()
        session_id = session["id"]

        # Add messages
        msg1 = research_service.add_message(session_id, "user", "First")
        msg2 = research_service.add_message(session_id, "assistant", "Second")
        msg3 = research_service.add_message(session_id, "user", "Third")

        # Retrieve
        messages = research_service.get_messages(session_id)

        # Verify order
        assert messages[0]["id"] == msg1["id"], "First message must be first"
        assert messages[1]["id"] == msg2["id"], "Second message must be second"
        assert messages[2]["id"] == msg3["id"], "Third message must be third"

    def test_role_constraint_enforced(self, research_service):
        """Test AC6: Role CHECK constraint allows only 'user' or 'assistant'."""
        session = research_service.get_default_session()
        session_id = session["id"]

        # Try to add message with invalid role
        with pytest.raises(Exception) as exc_info:
            research_service.add_message(session_id, "invalid_role", "Test")

        # Should raise constraint violation
        assert "constraint" in str(exc_info.value).lower() or "check" in str(
            exc_info.value
        ).lower(), "Invalid role should violate CHECK constraint"

    def test_foreign_key_constraint_enforced(self, research_service):
        """Test AC6: Foreign key constraint enforces session_id validity."""
        # Try to add message for non-existent session
        with pytest.raises(Exception) as exc_info:
            research_service.add_message("nonexistent_session", "user", "Test")

        # Should raise foreign key violation
        assert "foreign" in str(exc_info.value).lower() or "constraint" in str(
            exc_info.value
        ).lower(), "Invalid session_id should violate FOREIGN KEY constraint"

    # AC3: Session Folder Tests

    def test_default_session_folder_created(self, research_service):
        """Test AC3: Default session folder is created when session is accessed."""
        # Get default session (should auto-create folder)
        session = research_service.get_default_session()
        folder_path = Path(session["folder_path"])

        # Verify folder exists
        assert folder_path.exists(), \
            f"Session folder must be created at {folder_path}"
        assert folder_path.is_dir(), \
            "Session folder must be a directory"

    def test_session_folder_contains_softlink_to_source(self, research_service):
        """Test AC3: Session folder contains softlink to code-indexer source."""
        session = research_service.get_default_session()
        folder_path = Path(session["folder_path"])

        # Look for softlink named 'code-indexer' or 'source' in session folder
        softlink_path = folder_path / "code-indexer"

        assert softlink_path.exists(), \
            f"Softlink must exist at {softlink_path}"
        assert softlink_path.is_symlink(), \
            "code-indexer path must be a symbolic link"

        # Verify it points to valid location
        target = softlink_path.resolve()
        assert target.exists(), \
            "Softlink must point to existing directory"


class TestResearchSessionManagement:
    """Test Research Assistant Session Management (Story #143)."""

    @pytest.fixture
    def temp_db(self):
        """Create temporary database for testing."""
        import os
        # Create temp dir and database path
        temp_dir = tempfile.mkdtemp()
        db_path = os.path.join(temp_dir, "test.db")
        yield db_path
        # Cleanup
        Path(db_path).unlink(missing_ok=True)
        Path(temp_dir).rmdir()

    @pytest.fixture
    def research_service(self, temp_db):
        """Create ResearchAssistantService with temporary database."""
        from src.code_indexer.server.storage.database_manager import DatabaseSchema
        from src.code_indexer.server.services.research_assistant_service import (
            ResearchAssistantService,
        )

        # Initialize database schema first
        schema = DatabaseSchema(db_path=temp_db)
        schema.initialize_database()

        return ResearchAssistantService(db_path=temp_db)

    # AC2: Create New Session Tests

    def test_create_session_generates_uuid(self, research_service):
        """Test AC2: create_session creates a session with a UUID."""
        import uuid

        session = research_service.create_session()

        assert session is not None, "create_session must return a session"
        assert "id" in session, "Session must have an id"

        # Verify it's a valid UUID
        try:
            uuid.UUID(session["id"])
        except ValueError:
            pytest.fail(f"Session id '{session['id']}' is not a valid UUID")

    def test_create_session_creates_folder_and_softlink(self, research_service):
        """Test AC2: create_session creates folder at ~/.cidx-server/research/{uuid}/."""
        session = research_service.create_session()

        folder_path = Path(session["folder_path"])

        # Verify folder exists
        assert folder_path.exists(), \
            f"Session folder must be created at {folder_path}"
        assert folder_path.is_dir(), \
            "Session folder must be a directory"

        # Verify folder is in correct location
        expected_parent = Path.home() / ".cidx-server" / "research"
        assert folder_path.parent == expected_parent, \
            f"Session folder must be in {expected_parent}"

        # Verify softlink exists
        softlink = folder_path / "code-indexer"
        assert softlink.exists(), \
            f"Softlink must exist at {softlink}"
        assert softlink.is_symlink(), \
            "code-indexer path must be a symbolic link"

    def test_create_session_returns_full_dict(self, research_service):
        """Test AC2: create_session returns dict with all required fields."""
        session = research_service.create_session()

        # Verify all required fields present
        assert "id" in session, "Must have id"
        assert "name" in session, "Must have name"
        assert "folder_path" in session, "Must have folder_path"
        assert "created_at" in session, "Must have created_at"
        assert "updated_at" in session, "Must have updated_at"

    def test_create_session_default_name(self, research_service):
        """Test AC2: New session has 'New Session' name until first prompt."""
        session = research_service.create_session()

        assert session["name"] == "New Session", \
            "New session must have default name 'New Session'"

    # AC1: Get All Sessions Tests

    def test_get_all_sessions_returns_all(self, research_service):
        """Test AC1: get_all_sessions returns all sessions."""
        # Create multiple sessions
        session1 = research_service.create_session()
        session2 = research_service.create_session()
        session3 = research_service.create_session()

        sessions = research_service.get_all_sessions()

        assert len(sessions) == 3, "Must return all 3 sessions"

        # Verify all sessions are present
        session_ids = {s["id"] for s in sessions}
        assert session1["id"] in session_ids, "Session 1 must be in results"
        assert session2["id"] in session_ids, "Session 2 must be in results"
        assert session3["id"] in session_ids, "Session 3 must be in results"

    def test_get_all_sessions_ordered_by_updated_desc(self, research_service):
        """Test AC1: Sessions are ordered by updated_at DESC (most recent first)."""
        import time

        # Create sessions with slight time delays
        session1 = research_service.create_session()
        time.sleep(0.01)
        session2 = research_service.create_session()
        time.sleep(0.01)
        session3 = research_service.create_session()

        sessions = research_service.get_all_sessions()

        # Most recent should be first
        assert sessions[0]["id"] == session3["id"], \
            "Most recent session must be first"
        assert sessions[1]["id"] == session2["id"], \
            "Second most recent session must be second"
        assert sessions[2]["id"] == session1["id"], \
            "Oldest session must be last"

    def test_get_all_sessions_empty_list(self, research_service):
        """Test AC1: get_all_sessions returns empty list when no sessions exist."""
        sessions = research_service.get_all_sessions()

        assert sessions == [], \
            "Must return empty list when no sessions exist"

    def test_get_all_sessions_includes_all_fields(self, research_service):
        """Test AC1: Each session includes all required fields."""
        session = research_service.create_session()
        sessions = research_service.get_all_sessions()

        assert len(sessions) == 1, "Must have one session"

        s = sessions[0]
        assert "id" in s, "Must have id"
        assert "name" in s, "Must have name"
        assert "folder_path" in s, "Must have folder_path"
        assert "created_at" in s, "Must have created_at"
        assert "updated_at" in s, "Must have updated_at"

    # AC3: Get Single Session Tests

    def test_get_session_by_id(self, research_service):
        """Test AC3: get_session retrieves a specific session by ID."""
        # Create multiple sessions
        session1 = research_service.create_session()
        session2 = research_service.create_session()

        # Retrieve specific session
        retrieved = research_service.get_session(session1["id"])

        assert retrieved is not None, "Must retrieve session"
        assert retrieved["id"] == session1["id"], "Must retrieve correct session"
        assert retrieved["name"] == session1["name"], "Name must match"
        assert retrieved["folder_path"] == session1["folder_path"], \
            "Folder path must match"

    def test_get_session_not_found(self, research_service):
        """Test AC3: get_session returns None for non-existent session."""
        retrieved = research_service.get_session("nonexistent-uuid")

        assert retrieved is None, \
            "Must return None for non-existent session"

    # AC4: Rename Session Tests

    def test_rename_session_updates_name(self, research_service):
        """Test AC4: rename_session updates session name and updated_at timestamp."""
        import time

        session = research_service.create_session()
        original_updated_at = session["updated_at"]

        # Small delay to ensure timestamp difference
        time.sleep(0.01)

        # Rename session
        result = research_service.rename_session(session["id"], "My Investigation")

        assert result is True, "rename_session must return True on success"

        # Verify name was updated
        updated = research_service.get_session(session["id"])
        assert updated["name"] == "My Investigation", \
            "Session name must be updated in database"

        # Verify updated_at timestamp changed
        assert updated["updated_at"] != original_updated_at, \
            "updated_at timestamp must be updated on rename"

    def test_rename_session_validates_length(self, research_service):
        """Test AC4: rename_session validates name length (1-100 chars)."""
        session = research_service.create_session()

        # Test empty string
        result = research_service.rename_session(session["id"], "")
        assert result is False, "Must reject empty name"

        # Test too long (>100 chars)
        long_name = "a" * 101
        result = research_service.rename_session(session["id"], long_name)
        assert result is False, "Must reject name longer than 100 chars"

        # Test valid lengths
        result = research_service.rename_session(session["id"], "A")
        assert result is True, "Must accept 1 char name"

        result = research_service.rename_session(session["id"], "a" * 100)
        assert result is True, "Must accept 100 char name"

    def test_rename_session_validates_characters(self, research_service):
        """Test AC4: rename_session validates allowed characters."""
        session = research_service.create_session()

        # Valid: letters, numbers, spaces, hyphens
        valid_names = [
            "My Session",
            "Session-123",
            "Investigation 42",
            "Bug-Analysis-2024",
            "Test123 ABC-xyz"
        ]

        for name in valid_names:
            result = research_service.rename_session(session["id"], name)
            assert result is True, f"Must accept valid name: {name}"

        # Invalid: special characters
        invalid_names = [
            "Session@123",
            "Test/Session",
            "Bug#42",
            "Session_Test",  # Underscores not allowed
            "Test.Session",  # Dots not allowed
        ]

        for name in invalid_names:
            result = research_service.rename_session(session["id"], name)
            assert result is False, f"Must reject invalid name: {name}"

    def test_rename_session_not_found(self, research_service):
        """Test AC4: rename_session returns False for non-existent session."""
        result = research_service.rename_session("nonexistent-uuid", "New Name")

        assert result is False, \
            "Must return False for non-existent session"

    # AC2/AC4: Generate Session Name Tests

    def test_generate_session_name_first_50_chars(self, research_service):
        """Test AC2/AC4: generate_session_name takes first 50 chars."""
        # Prompt longer than 50 chars
        long_prompt = "This is a very long prompt that goes on and on for more than fifty characters total"

        name = research_service.generate_session_name(long_prompt)

        assert len(name) == 50, "Must truncate to 50 chars"
        assert name == long_prompt[:50], "Must take first 50 chars"

    def test_generate_session_name_strips_newlines(self, research_service):
        """Test AC2/AC4: generate_session_name removes newlines and carriage returns."""
        prompt_with_newlines = "Line 1\nLine 2\rLine 3\r\nLine 4"

        name = research_service.generate_session_name(prompt_with_newlines)

        assert "\n" not in name, "Must remove newlines"
        assert "\r" not in name, "Must remove carriage returns"
        assert name == "Line 1 Line 2 Line 3 Line 4", \
            "Must replace newlines with spaces"

    def test_generate_session_name_empty(self, research_service):
        """Test AC2/AC4: generate_session_name returns 'New Session' for empty input."""
        # Empty string
        name = research_service.generate_session_name("")
        assert name == "New Session", "Must return 'New Session' for empty string"

        # Only whitespace
        name = research_service.generate_session_name("   \n\r  ")
        assert name == "New Session", \
            "Must return 'New Session' for whitespace-only string"

    # AC5: Delete Session Tests

    def test_delete_session_removes_from_database(self, research_service):
        """Test AC5: delete_session removes session from database."""
        session = research_service.create_session()

        # Delete session
        result = research_service.delete_session(session["id"])

        assert result is True, "delete_session must return True on success"

        # Verify session no longer exists in database
        deleted = research_service.get_session(session["id"])
        assert deleted is None, "Session must be removed from database"

    def test_delete_session_cascades_messages(self, research_service):
        """Test AC5: delete_session CASCADE deletes associated messages."""
        session = research_service.create_session()

        # Add messages to session
        research_service.add_message(session["id"], "user", "Question 1")
        research_service.add_message(session["id"], "assistant", "Answer 1")

        # Verify messages exist
        messages_before = research_service.get_messages(session["id"])
        assert len(messages_before) == 2, "Messages must exist before deletion"

        # Delete session
        result = research_service.delete_session(session["id"])
        assert result is True, "Delete must succeed"

        # Verify messages are also deleted (CASCADE)
        messages_after = research_service.get_messages(session["id"])
        assert len(messages_after) == 0, \
            "Messages must be CASCADE deleted with session"

    def test_delete_session_removes_folder(self, research_service):
        """Test AC5: delete_session removes session folder from filesystem."""
        session = research_service.create_session()
        folder_path = Path(session["folder_path"])

        # Verify folder exists before deletion
        assert folder_path.exists(), "Folder must exist before deletion"

        # Delete session
        result = research_service.delete_session(session["id"])
        assert result is True, "Delete must succeed"

        # Verify folder is removed
        assert not folder_path.exists(), \
            "Session folder must be removed from filesystem"

    def test_delete_session_not_found(self, research_service):
        """Test AC5: delete_session returns False for non-existent session."""
        result = research_service.delete_session("nonexistent-uuid")

        assert result is False, \
            "Must return False for non-existent session"


class TestFileUploadService:
    """Test Research Assistant File Upload (Story #144)."""

    @pytest.fixture
    def temp_db(self):
        """Create temporary database for testing."""
        import os
        temp_dir = tempfile.mkdtemp()
        db_path = os.path.join(temp_dir, "test.db")
        yield db_path
        Path(db_path).unlink(missing_ok=True)
        Path(temp_dir).rmdir()

    @pytest.fixture
    def research_service(self, temp_db):
        """Create ResearchAssistantService with temporary database."""
        from src.code_indexer.server.storage.database_manager import DatabaseSchema
        from src.code_indexer.server.services.research_assistant_service import (
            ResearchAssistantService,
        )

        schema = DatabaseSchema(db_path=temp_db)
        schema.initialize_database()

        return ResearchAssistantService(db_path=temp_db)

    # AC2: Filename Sanitization Tests

    def test_sanitize_filename_removes_path_separators(self, research_service):
        """Test AC2: sanitize_filename removes path separators."""
        # Test forward slash
        result = research_service.sanitize_filename("path/to/file.txt")
        assert "/" not in result, "Must remove forward slashes"

        # Test backslash
        result = research_service.sanitize_filename("path\\to\\file.txt")
        assert "\\" not in result, "Must remove backslashes"

    def test_sanitize_filename_removes_null_bytes(self, research_service):
        """Test AC2: sanitize_filename removes null bytes and control chars."""
        result = research_service.sanitize_filename("file\x00name.txt")
        assert "\x00" not in result, "Must remove null bytes"

        result = research_service.sanitize_filename("file\x01\x02name.txt")
        assert "\x01" not in result and "\x02" not in result, \
            "Must remove control characters"

    def test_sanitize_filename_replaces_spaces_with_underscores(self, research_service):
        """Test AC2: sanitize_filename replaces spaces with underscores."""
        result = research_service.sanitize_filename("my file name.txt")
        assert " " not in result, "Must remove spaces"
        assert "my_file_name.txt" == result, "Must replace spaces with underscores"

    def test_sanitize_filename_limits_length(self, research_service):
        """Test AC2: sanitize_filename limits filename to 255 chars."""
        long_name = "a" * 300 + ".txt"
        result = research_service.sanitize_filename(long_name)
        assert len(result) <= 255, "Must limit to 255 chars"

    def test_sanitize_filename_preserves_extension(self, research_service):
        """Test AC2: sanitize_filename preserves file extension."""
        result = research_service.sanitize_filename("my file.log")
        assert result.endswith(".log"), "Must preserve extension"

        result = research_service.sanitize_filename("config file.json")
        assert result.endswith(".json"), "Must preserve extension"

    # AC2: Duplicate Filename Handling Tests

    def test_get_unique_filename_no_collision(self, research_service):
        """Test AC2: get_unique_filename returns original name if no collision."""
        import os
        temp_dir = tempfile.mkdtemp()
        try:
            upload_dir = Path(temp_dir)

            result = research_service.get_unique_filename(upload_dir, "file.txt")
            assert result == "file.txt", \
                "Must return original name when no collision"
        finally:
            os.rmdir(temp_dir)

    def test_get_unique_filename_handles_collision(self, research_service):
        """Test AC2: get_unique_filename adds suffix for duplicates."""
        import os
        temp_dir = tempfile.mkdtemp()
        try:
            upload_dir = Path(temp_dir)

            # Create existing file
            (upload_dir / "file.txt").touch()

            result = research_service.get_unique_filename(upload_dir, "file.txt")
            assert result == "file_1.txt", \
                "Must add _1 suffix for first duplicate"

            # Create second duplicate
            (upload_dir / "file_1.txt").touch()

            result = research_service.get_unique_filename(upload_dir, "file.txt")
            assert result == "file_2.txt", \
                "Must add _2 suffix for second duplicate"
        finally:
            import shutil
            shutil.rmtree(temp_dir)

    # AC2: File Upload and Storage Tests

    def test_upload_file_creates_uploads_folder(self, research_service):
        """Test AC2: upload_file creates uploads folder if it doesn't exist."""
        import io
        from fastapi import UploadFile

        session = research_service.create_session()
        uploads_dir = Path(session["folder_path"]) / "uploads"

        # Verify uploads folder doesn't exist yet
        if uploads_dir.exists():
            import shutil
            shutil.rmtree(uploads_dir)

        # Create mock file
        content = b"test content"
        file = UploadFile(filename="test.txt", file=io.BytesIO(content))

        result = research_service.upload_file(session["id"], file)

        # Verify uploads folder was created
        assert uploads_dir.exists(), "Must create uploads folder"
        assert uploads_dir.is_dir(), "uploads must be a directory"

    def test_upload_file_saves_with_sanitized_name(self, research_service):
        """Test AC2: upload_file saves file with sanitized filename."""
        import io
        from fastapi import UploadFile

        session = research_service.create_session()
        uploads_dir = Path(session["folder_path"]) / "uploads"

        # File with spaces and special chars
        content = b"test content"
        file = UploadFile(filename="my file name.txt", file=io.BytesIO(content))

        result = research_service.upload_file(session["id"], file)

        assert result["success"] is True, "Upload must succeed"
        assert result["filename"] == "my_file_name.txt", \
            "Must use sanitized filename"

        # Verify file exists on filesystem
        saved_file = uploads_dir / "my_file_name.txt"
        assert saved_file.exists(), "File must be saved to filesystem"

    def test_upload_file_handles_duplicate_names(self, research_service):
        """Test AC2: upload_file handles duplicate filenames with suffix."""
        import io
        from fastapi import UploadFile

        session = research_service.create_session()
        uploads_dir = Path(session["folder_path"]) / "uploads"

        # Upload first file
        content1 = b"content 1"
        file1 = UploadFile(filename="file.txt", file=io.BytesIO(content1))
        result1 = research_service.upload_file(session["id"], file1)
        assert result1["filename"] == "file.txt", "First upload uses original name"

        # Upload duplicate
        content2 = b"content 2"
        file2 = UploadFile(filename="file.txt", file=io.BytesIO(content2))
        result2 = research_service.upload_file(session["id"], file2)
        assert result2["filename"] == "file_1.txt", \
            "Duplicate upload must get _1 suffix"

        # Verify both files exist
        assert (uploads_dir / "file.txt").exists(), "First file must exist"
        assert (uploads_dir / "file_1.txt").exists(), "Second file must exist"

    # AC6: File Type Restrictions Tests

    def test_upload_file_allows_text_extensions(self, research_service):
        """Test AC6: upload_file allows .txt files."""
        import io
        from fastapi import UploadFile

        session = research_service.create_session()

        content = b"test content"
        file = UploadFile(filename="test.txt", file=io.BytesIO(content))

        result = research_service.upload_file(session["id"], file)

        assert result["success"] is True, "Must allow .txt files"

    def test_upload_file_allows_log_extensions(self, research_service):
        """Test AC6: upload_file allows .log files."""
        import io
        from fastapi import UploadFile

        session = research_service.create_session()

        content = b"log content"
        file = UploadFile(filename="app.log", file=io.BytesIO(content))

        result = research_service.upload_file(session["id"], file)

        assert result["success"] is True, "Must allow .log files"

    def test_upload_file_allows_all_specified_extensions(self, research_service):
        """Test AC6: upload_file allows all specified extensions."""
        import io
        from fastapi import UploadFile

        session = research_service.create_session()

        allowed_extensions = [
            '.txt', '.log', '.json', '.yaml', '.yml', '.py', '.md',
            '.csv', '.xml', '.html', '.cfg', '.conf', '.ini'
        ]

        for ext in allowed_extensions:
            content = b"test content"
            file = UploadFile(filename=f"test{ext}", file=io.BytesIO(content))

            result = research_service.upload_file(session["id"], file)

            assert result["success"] is True, f"Must allow {ext} files"

    def test_upload_file_rejects_executable_extensions(self, research_service):
        """Test AC6: upload_file rejects executable files."""
        import io
        from fastapi import UploadFile

        session = research_service.create_session()

        rejected_extensions = ['.exe', '.sh', '.bat', '.com', '.dll']

        for ext in rejected_extensions:
            content = b"malicious content"
            file = UploadFile(filename=f"malware{ext}", file=io.BytesIO(content))

            result = research_service.upload_file(session["id"], file)

            assert result["success"] is False, f"Must reject {ext} files"
            assert "not allowed" in result["error"].lower() or \
                   "rejected" in result["error"].lower(), \
                f"Error message must explain rejection for {ext}"

    def test_upload_file_rejects_archive_extensions(self, research_service):
        """Test AC6: upload_file rejects archive files."""
        import io
        from fastapi import UploadFile

        session = research_service.create_session()

        rejected_extensions = ['.zip', '.tar', '.gz', '.rar', '.7z']

        for ext in rejected_extensions:
            content = b"archive content"
            file = UploadFile(filename=f"archive{ext}", file=io.BytesIO(content))

            result = research_service.upload_file(session["id"], file)

            assert result["success"] is False, f"Must reject {ext} files"

    # AC6: File Size Limit Tests

    def test_upload_file_enforces_max_file_size(self, research_service):
        """Test AC6: upload_file rejects files larger than 10MB."""
        import io
        from fastapi import UploadFile

        session = research_service.create_session()

        # Create file larger than 10MB
        large_content = b"x" * (11 * 1024 * 1024)  # 11MB
        file = UploadFile(filename="large.txt", file=io.BytesIO(large_content))

        result = research_service.upload_file(session["id"], file)

        assert result["success"] is False, "Must reject file larger than 10MB"
        assert "size" in result["error"].lower() or "10" in result["error"].lower(), \
            "Error message must mention size limit"

    def test_upload_file_enforces_max_session_size(self, research_service):
        """Test AC6: upload_file rejects files that would exceed 100MB session limit."""
        import io
        from fastapi import UploadFile

        session = research_service.create_session()

        # Upload files totaling 95MB (10 files x 9.5MB each)
        for i in range(10):
            content = b"x" * int(9.5 * 1024 * 1024)  # 9.5MB each
            file = UploadFile(filename=f"file{i}.txt", file=io.BytesIO(content))
            result = research_service.upload_file(session["id"], file)
            assert result["success"] is True, f"File {i} should succeed (under 100MB total)"

        # Try to upload one more file (would exceed 100MB)
        content = b"x" * (6 * 1024 * 1024)  # 6MB (total would be 101MB)
        file = UploadFile(filename="final.txt", file=io.BytesIO(content))
        result = research_service.upload_file(session["id"], file)

        assert result["success"] is False, \
            "Must reject file that would exceed 100MB session limit"
        assert "session" in result["error"].lower() and \
               ("limit" in result["error"].lower() or "100" in result["error"]), \
            "Error message must mention session limit"

    # AC4: List/Delete/Get File Tests

    def test_list_files_returns_uploaded_files(self, research_service):
        """Test AC4: list_files returns all uploaded files with metadata."""
        import io
        from fastapi import UploadFile

        session = research_service.create_session()

        # Upload files
        file1 = UploadFile(filename="file1.txt", file=io.BytesIO(b"content 1"))
        file2 = UploadFile(filename="file2.log", file=io.BytesIO(b"content 2"))

        research_service.upload_file(session["id"], file1)
        research_service.upload_file(session["id"], file2)

        # List files
        files = research_service.list_files(session["id"])

        assert len(files) == 2, "Must list all uploaded files"

        # Verify metadata present
        for file_info in files:
            assert "filename" in file_info, "Must include filename"
            assert "size" in file_info, "Must include size"
            assert "uploaded_at" in file_info, "Must include upload time"

    def test_list_files_empty_when_no_uploads(self, research_service):
        """Test AC4: list_files returns empty list when no files uploaded."""
        session = research_service.create_session()

        files = research_service.list_files(session["id"])

        assert files == [], "Must return empty list when no files uploaded"

    def test_delete_file_removes_file(self, research_service):
        """Test AC4: delete_file removes file from filesystem."""
        import io
        from fastapi import UploadFile

        session = research_service.create_session()
        uploads_dir = Path(session["folder_path"]) / "uploads"

        # Upload file
        file = UploadFile(filename="test.txt", file=io.BytesIO(b"content"))
        result = research_service.upload_file(session["id"], file)
        filename = result["filename"]

        # Verify file exists
        file_path = uploads_dir / filename
        assert file_path.exists(), "File must exist before deletion"

        # Delete file
        success = research_service.delete_file(session["id"], filename)

        assert success is True, "delete_file must return True on success"
        assert not file_path.exists(), "File must be removed from filesystem"

    def test_delete_file_not_found(self, research_service):
        """Test AC4: delete_file returns False for non-existent file."""
        session = research_service.create_session()

        success = research_service.delete_file(session["id"], "nonexistent.txt")

        assert success is False, "Must return False for non-existent file"

    def test_get_file_path_returns_path(self, research_service):
        """Test AC4: get_file_path returns path to uploaded file."""
        import io
        from fastapi import UploadFile

        session = research_service.create_session()

        # Upload file
        file = UploadFile(filename="test.txt", file=io.BytesIO(b"content"))
        result = research_service.upload_file(session["id"], file)
        filename = result["filename"]

        # Get file path
        file_path = research_service.get_file_path(session["id"], filename)

        assert file_path is not None, "Must return path for existing file"
        assert file_path.exists(), "Returned path must exist"
        assert file_path.name == filename, "Path must point to correct file"

    def test_get_file_path_not_found(self, research_service):
        """Test AC4: get_file_path returns None for non-existent file."""
        session = research_service.create_session()

        file_path = research_service.get_file_path(session["id"], "nonexistent.txt")

        assert file_path is None, "Must return None for non-existent file"
