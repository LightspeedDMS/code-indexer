"""
Unit tests for Research Assistant File Upload Router (Story #144).

Tests router endpoints for file upload, list, delete, and download.
"""

import pytest
import tempfile
import io
from pathlib import Path
from fastapi.testclient import TestClient


class TestFileUploadRouter:
    """Test file upload router endpoints (Story #144)."""

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
    def client(self, temp_db, monkeypatch):
        """Create test client with temporary database."""
        from src.code_indexer.server.storage.database_manager import DatabaseSchema
        from src.code_indexer.server.app import app
        from src.code_indexer.server.web.auth import require_admin_session, SessionData

        # Initialize database
        schema = DatabaseSchema(db_path=temp_db)
        schema.initialize_database()

        # Monkeypatch ResearchAssistantService to use temp_db
        monkeypatch.setenv("CIDX_SERVER_DATA_DIR", str(Path(temp_db).parent.parent))

        # Mock admin session
        def mock_require_admin_session():
            return SessionData(username="admin")

        app.dependency_overrides[require_admin_session] = mock_require_admin_session

        # Create test client
        client = TestClient(app)

        yield client

        # Cleanup
        app.dependency_overrides.clear()

    @pytest.fixture
    def session_id(self, client, temp_db):
        """Create a test session."""
        from src.code_indexer.server.services.research_assistant_service import (
            ResearchAssistantService
        )
        service = ResearchAssistantService(db_path=temp_db)
        session = service.create_session()
        return session["id"]

    def test_upload_file_endpoint(self, client, session_id):
        """Test POST /sessions/{id}/upload endpoint."""
        # Create test file
        file_content = b"test file content"
        files = {"file": ("test.txt", io.BytesIO(file_content), "text/plain")}

        response = client.post(
            f"/admin/research/sessions/{session_id}/upload",
            files=files
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True
        assert data["filename"] == "test.txt"
        assert data["size"] == len(file_content)
        assert "uploaded_at" in data

    def test_list_files_endpoint(self, client, session_id):
        """Test GET /sessions/{id}/files endpoint."""
        # Upload a file first
        file_content = b"content"
        files = {"file": ("file1.txt", io.BytesIO(file_content), "text/plain")}
        client.post(f"/admin/research/sessions/{session_id}/upload", files=files)

        # List files
        response = client.get(f"/admin/research/sessions/{session_id}/files")

        assert response.status_code == 200
        data = response.json()
        assert "files" in data
        assert len(data["files"]) == 1
        assert data["files"][0]["filename"] == "file1.txt"
        assert data["files"][0]["size"] == len(file_content)

    def test_delete_file_endpoint(self, client, session_id):
        """Test DELETE /sessions/{id}/files/{filename} endpoint."""
        # Upload a file first
        file_content = b"content to delete"
        files = {"file": ("delete_me.txt", io.BytesIO(file_content), "text/plain")}
        upload_response = client.post(
            f"/admin/research/sessions/{session_id}/upload",
            files=files
        )
        assert upload_response.status_code == 200

        # Delete the file
        response = client.delete(
            f"/admin/research/sessions/{session_id}/files/delete_me.txt"
        )

        assert response.status_code == 200
        data = response.json()
        assert data["success"] is True

    def test_download_file_endpoint(self, client, session_id):
        """Test GET /sessions/{id}/files/{filename} endpoint for download."""
        # Upload a file first
        file_content = b"content to download"
        files = {"file": ("download.txt", io.BytesIO(file_content), "text/plain")}
        upload_response = client.post(
            f"/admin/research/sessions/{session_id}/upload",
            files=files
        )
        assert upload_response.status_code == 200

        # Download the file
        response = client.get(
            f"/admin/research/sessions/{session_id}/files/download.txt"
        )

        assert response.status_code == 200
        assert response.content == file_content
