"""
Tests for ResearchSessionsBackend Protocol and ResearchSessionsSqliteBackend (Story #522).

Covers:
- Protocol is runtime-checkable
- SQLite backend satisfies protocol
- create_session + get_session round trip
- list_sessions returns all
- delete_session removes record
- update_session_title works
- add_message + get_messages round trip
- BackendRegistry has research_sessions field
"""

import pytest


# ---------------------------------------------------------------------------
# Protocol tests
# ---------------------------------------------------------------------------


class TestResearchSessionsBackendProtocol:
    """Tests for the ResearchSessionsBackend Protocol definition."""

    def test_protocol_is_runtime_checkable(self):
        """ResearchSessionsBackend must be decorated with @runtime_checkable."""
        from code_indexer.server.storage.protocols import ResearchSessionsBackend

        assert hasattr(ResearchSessionsBackend, "__protocol_attrs__") or hasattr(
            ResearchSessionsBackend, "_is_protocol"
        ), "ResearchSessionsBackend must be a Protocol"

        class NotABackend:
            pass

        try:
            isinstance(NotABackend(), ResearchSessionsBackend)
        except TypeError:
            pytest.fail(
                "isinstance() raised TypeError — ResearchSessionsBackend is not @runtime_checkable"
            )

    def test_protocol_has_required_methods(self):
        """ResearchSessionsBackend Protocol must declare all required methods."""
        from code_indexer.server.storage.protocols import ResearchSessionsBackend

        attrs = dir(ResearchSessionsBackend)
        for method in [
            "create_session",
            "get_session",
            "list_sessions",
            "delete_session",
            "update_session_title",
            "update_session_claude_id",
            "add_message",
            "get_messages",
            "close",
        ]:
            assert method in attrs, f"ResearchSessionsBackend must have {method}()"


# ---------------------------------------------------------------------------
# SQLite backend tests
# ---------------------------------------------------------------------------


class TestResearchSessionsSqliteBackend:
    """Tests for ResearchSessionsSqliteBackend implementation."""

    @pytest.fixture
    def db_path(self, tmp_path):
        """Provide a temp path for the test database."""
        return str(tmp_path / "test_research.db")

    @pytest.fixture
    def backend(self, db_path):
        """Create a fresh ResearchSessionsSqliteBackend for each test."""
        from code_indexer.server.storage.sqlite_backends import (
            ResearchSessionsSqliteBackend,
        )

        b = ResearchSessionsSqliteBackend(db_path)
        yield b
        b.close()

    def test_sqlite_backend_satisfies_protocol(self, db_path):
        """isinstance(ResearchSessionsSqliteBackend(...), ResearchSessionsBackend) must be True."""
        from code_indexer.server.storage.sqlite_backends import (
            ResearchSessionsSqliteBackend,
        )
        from code_indexer.server.storage.protocols import ResearchSessionsBackend

        b = ResearchSessionsSqliteBackend(db_path)
        assert isinstance(b, ResearchSessionsBackend), (
            "ResearchSessionsSqliteBackend must satisfy the ResearchSessionsBackend Protocol"
        )
        b.close()

    def test_create_session_and_get_session_round_trip(self, backend):
        """create_session followed by get_session returns the stored record."""
        backend.create_session(
            session_id="sess-1",
            name="Test Session",
            folder_path="/tmp/test",
        )
        result = backend.get_session("sess-1")
        assert result is not None
        assert result["id"] == "sess-1"
        assert result["name"] == "Test Session"
        assert result["folder_path"] == "/tmp/test"
        assert result["claude_session_id"] is None

    def test_get_session_returns_none_when_not_found(self, backend):
        """get_session returns None for non-existent session ID."""
        assert backend.get_session("nonexistent") is None

    def test_list_sessions_returns_all(self, backend):
        """list_sessions returns all created sessions."""
        backend.create_session("s1", "Session 1", "/tmp/s1")
        backend.create_session("s2", "Session 2", "/tmp/s2")
        sessions = backend.list_sessions()
        ids = {s["id"] for s in sessions}
        assert "s1" in ids
        assert "s2" in ids

    def test_list_sessions_empty_when_none(self, backend):
        """list_sessions returns empty list when no sessions exist."""
        assert backend.list_sessions() == []

    def test_delete_session_removes_record(self, backend):
        """delete_session returns True and removes the session."""
        backend.create_session("del-sess", "To Delete", "/tmp/del")
        assert backend.get_session("del-sess") is not None
        result = backend.delete_session("del-sess")
        assert result is True
        assert backend.get_session("del-sess") is None

    def test_delete_session_returns_false_when_not_found(self, backend):
        """delete_session returns False for non-existent session."""
        assert backend.delete_session("nonexistent") is False

    def test_update_session_title_works(self, backend):
        """update_session_title updates the name field."""
        backend.create_session("title-sess", "Original Name", "/tmp/title")
        result = backend.update_session_title("title-sess", "New Name")
        assert result is True
        session = backend.get_session("title-sess")
        assert session is not None
        assert session["name"] == "New Name"

    def test_update_session_title_returns_false_when_not_found(self, backend):
        """update_session_title returns False for non-existent session."""
        assert backend.update_session_title("nonexistent", "Name") is False

    def test_update_session_claude_id(self, backend):
        """update_session_claude_id stores the claude_session_id."""
        backend.create_session("claude-sess", "Claude Session", "/tmp/claude")
        backend.update_session_claude_id("claude-sess", "claude-uuid-123")
        session = backend.get_session("claude-sess")
        assert session is not None
        assert session["claude_session_id"] == "claude-uuid-123"

    def test_add_message_and_get_messages_round_trip(self, backend):
        """add_message stores message; get_messages retrieves it in order."""
        backend.create_session("msg-sess", "Msg Session", "/tmp/msg")
        msg1 = backend.add_message("msg-sess", "user", "Hello")
        msg2 = backend.add_message("msg-sess", "assistant", "World")

        assert msg1["role"] == "user"
        assert msg1["content"] == "Hello"
        assert msg2["role"] == "assistant"

        messages = backend.get_messages("msg-sess")
        assert len(messages) == 2
        assert messages[0]["content"] == "Hello"
        assert messages[1]["content"] == "World"

    def test_get_messages_returns_empty_for_unknown_session(self, backend):
        """get_messages returns empty list for non-existent session."""
        assert backend.get_messages("nonexistent") == []


# ---------------------------------------------------------------------------
# BackendRegistry wiring test
# ---------------------------------------------------------------------------


class TestBackendRegistryResearchSessions:
    """Tests that BackendRegistry includes research_sessions field."""

    def test_backend_registry_has_research_sessions_field(self):
        """BackendRegistry dataclass must declare a research_sessions field."""
        from code_indexer.server.storage.factory import BackendRegistry
        import dataclasses

        field_names = {f.name for f in dataclasses.fields(BackendRegistry)}
        assert "research_sessions" in field_names, (
            "BackendRegistry must have a research_sessions field"
        )

    def test_storage_factory_creates_research_sessions_in_sqlite_mode(self, tmp_path):
        """StorageFactory._create_sqlite_backends creates a research_sessions backend."""
        from code_indexer.server.storage.factory import StorageFactory
        from code_indexer.server.storage.protocols import ResearchSessionsBackend
        import os

        data_dir = str(tmp_path / "data")
        os.makedirs(data_dir, exist_ok=True)

        registry = StorageFactory._create_sqlite_backends(data_dir)
        assert hasattr(registry, "research_sessions")
        assert isinstance(registry.research_sessions, ResearchSessionsBackend), (
            "StorageFactory must create a ResearchSessionsBackend for research_sessions"
        )
