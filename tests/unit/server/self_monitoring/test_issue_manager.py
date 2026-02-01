"""
Unit tests for IssueManager (Story #73 - AC1, AC5c).

Tests issue creation, metadata storage, and deduplication support:
- GitHub issue creation via gh CLI
- SQLite metadata storage on successful creation
- Fingerprint computation for deduplication
- Error code extraction from issue titles
"""

import pytest
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import Mock, patch
from datetime import datetime


class TestIssueManager:
    """Test suite for IssueManager issue creation and metadata storage."""

    @pytest.fixture
    def temp_db(self):
        """Create temporary database for testing."""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as f:
            db_path = f.name

        # Initialize database with self_monitoring_issues table
        conn = sqlite3.connect(db_path)
        conn.execute("""
            CREATE TABLE self_monitoring_issues (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                scan_id TEXT NOT NULL,
                github_issue_number INTEGER,
                github_issue_url TEXT,
                classification TEXT NOT NULL,
                error_codes TEXT,
                fingerprint TEXT NOT NULL,
                source_log_ids TEXT NOT NULL,
                source_files TEXT,
                title TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
        """)
        conn.commit()
        conn.close()

        yield db_path

        # Cleanup
        Path(db_path).unlink(missing_ok=True)

    @pytest.fixture
    def mock_httpx_post(self):
        """Mock httpx.post for GitHub API calls."""
        with patch("code_indexer.server.self_monitoring.issue_manager.httpx.post") as mock_post:
            yield mock_post

    def test_create_issue_calls_github_api(self, temp_db, mock_httpx_post):
        """Test IssueManager calls GitHub REST API to create GitHub issue."""
        from code_indexer.server.self_monitoring.issue_manager import IssueManager

        # Mock successful GitHub API response
        mock_httpx_post.return_value = Mock(
            status_code=201,
            json=lambda: {
                "number": 123,
                "html_url": "https://github.com/owner/repo/issues/123"
            },
            headers={},
            raise_for_status=lambda: None
        )

        manager = IssueManager(
            db_path=temp_db,
            scan_id="test-scan-001",
            github_repo="owner/repo",
            github_token="ghp_test_token"
        )

        issue_data = manager.create_issue(
            classification="server_bug",
            title="[BUG] Authentication token validation failed",
            body="## Description\nToken validation error...",
            source_log_ids=[1001, 1002, 1003],
            source_files=["auth/token_validator.py"],
            error_codes=["AUTH-TOKEN-001"]
        )

        # Verify GitHub API was called correctly
        assert mock_httpx_post.call_count >= 1
        call_kwargs = mock_httpx_post.call_args[1]

        # Verify URL
        call_url = mock_httpx_post.call_args[0][0]
        assert "https://api.github.com/repos/owner/repo/issues" == call_url

        # Verify headers include Bearer token
        assert "headers" in call_kwargs
        assert "Authorization" in call_kwargs["headers"]
        assert call_kwargs["headers"]["Authorization"] == "Bearer ghp_test_token"

        # Verify JSON payload
        assert "json" in call_kwargs
        assert call_kwargs["json"]["title"] == "[BUG] Authentication token validation failed"

        # Verify returned issue data
        assert issue_data["github_issue_number"] == 123
        assert issue_data["github_issue_url"] == "https://github.com/owner/repo/issues/123"
        assert issue_data["classification"] == "server_bug"

    def test_create_issue_stores_metadata_in_db(self, temp_db, mock_httpx_post):
        """Test IssueManager stores issue metadata in SQLite database."""
        from code_indexer.server.self_monitoring.issue_manager import IssueManager

        # Mock successful GitHub API response
        mock_httpx_post.return_value = Mock(
            status_code=201,
            json=lambda: {
                "number": 456,
                "html_url": "https://github.com/owner/repo/issues/456"
            },
            headers={},
            raise_for_status=lambda: None
        )

        manager = IssueManager(
            db_path=temp_db,
            scan_id="test-scan-002",
            github_repo="owner/repo",
            github_token="ghp_test_token"
        )

        manager.create_issue(
            classification="client_misuse",
            title="[CLIENT] Invalid query parameter format",
            body="## Description\nClient sent malformed request...",
            source_log_ids=[2001, 2002],
            source_files=["api/query_handler.py", "api/validators.py"],
            error_codes=["QUERY-PARAM-001"]
        )

        # Verify metadata was stored in database
        conn = sqlite3.connect(temp_db)
        cursor = conn.execute(
            "SELECT scan_id, github_issue_number, github_issue_url, "
            "classification, error_codes, source_log_ids, source_files, title "
            "FROM self_monitoring_issues WHERE github_issue_number = ?",
            (456,)
        )
        row = cursor.fetchone()
        conn.close()

        assert row is not None
        assert row[0] == "test-scan-002"  # scan_id
        assert row[1] == 456  # github_issue_number
        assert row[2] == "https://github.com/owner/repo/issues/456"
        assert row[3] == "client_misuse"  # classification
        assert row[4] == "QUERY-PARAM-001"  # error_codes (single)
        assert row[5] == "2001,2002"  # source_log_ids (CSV)
        assert "api/query_handler.py" in row[6]  # source_files
        assert row[7] == "[CLIENT] Invalid query parameter format"  # title

    def test_create_issue_handles_github_api_failure(self, temp_db, mock_httpx_post):
        """Test IssueManager handles GitHub API failure gracefully."""
        from code_indexer.server.self_monitoring.issue_manager import IssueManager
        import httpx

        # Mock GitHub API failure
        mock_response = Mock()
        mock_response.status_code = 403
        mock_response.headers = {"X-RateLimit-Remaining": "0"}

        def raise_http_error():
            raise httpx.HTTPStatusError("API rate limit exceeded", request=Mock(), response=mock_response)

        mock_response.raise_for_status = raise_http_error
        mock_httpx_post.return_value = mock_response

        manager = IssueManager(
            db_path=temp_db,
            scan_id="test-scan-007",
            github_repo="owner/repo",
            github_token="ghp_test_token"
        )

        # Should raise exception on failure
        with pytest.raises(RuntimeError, match="GitHub API rate limit exceeded"):
            manager.create_issue(
                classification="server_bug",
                title="[BUG] Test issue",
                body="Test body",
                source_log_ids=[9999],
                source_files=["test.py"],
                error_codes=[]
            )

        # Should NOT store metadata in database on failure
        conn = sqlite3.connect(temp_db)
        cursor = conn.execute(
            "SELECT COUNT(*) FROM self_monitoring_issues WHERE scan_id = ?",
            ("test-scan-007",)
        )
        count = cursor.fetchone()[0]
        conn.close()

        assert count == 0

    def test_create_issue_computes_fingerprint(self, temp_db, mock_httpx_post):
        """Test IssueManager computes deterministic fingerprint for deduplication."""
        from code_indexer.server.self_monitoring.issue_manager import IssueManager

        # Mock successful GitHub API response
        mock_httpx_post.return_value = Mock(
            status_code=201,
            json=lambda: {
                "number": 789,
                "html_url": "https://github.com/owner/repo/issues/789"
            },
            headers={},
            raise_for_status=lambda: None
        )

        manager = IssueManager(
            db_path=temp_db,
            scan_id="test-scan-003",
            github_repo="owner/repo",
            github_token="ghp_test_token"
        )

        manager.create_issue(
            classification="server_bug",
            title="[BUG] Connection pool exhausted",
            body="## Description\nDatabase connection pool exhausted...",
            source_log_ids=[3001],
            source_files=["db/connection_pool.py"],
            error_codes=["DB-POOL-001"]
        )

        # Retrieve fingerprint from database
        conn = sqlite3.connect(temp_db)
        cursor = conn.execute(
            "SELECT fingerprint FROM self_monitoring_issues WHERE github_issue_number = ?",
            (789,)
        )
        row = cursor.fetchone()
        conn.close()

        assert row is not None
        fingerprint = row[0]

        # Fingerprint should be non-empty hash
        assert len(fingerprint) > 0
        # Should be hex-encoded hash
        assert all(c in "0123456789abcdef" for c in fingerprint)

    def test_fingerprint_is_deterministic(self, temp_db):
        """Test fingerprint computation is deterministic for same inputs."""
        from code_indexer.server.self_monitoring.issue_manager import IssueManager

        manager = IssueManager(
            db_path=temp_db,
            scan_id="test-scan-004",
            github_repo="owner/repo"
        )

        # Compute fingerprint twice with same inputs
        fingerprint1 = manager.compute_fingerprint(
            classification="server_bug",
            source_files=["auth/validator.py"],
            error_type="ValidationError"
        )

        fingerprint2 = manager.compute_fingerprint(
            classification="server_bug",
            source_files=["auth/validator.py"],
            error_type="ValidationError"
        )

        assert fingerprint1 == fingerprint2

    def test_fingerprint_differs_for_different_inputs(self, temp_db):
        """Test fingerprint differs when inputs change."""
        from code_indexer.server.self_monitoring.issue_manager import IssueManager

        manager = IssueManager(
            db_path=temp_db,
            scan_id="test-scan-005",
            github_repo="owner/repo"
        )

        # Different classification
        fp1 = manager.compute_fingerprint(
            classification="server_bug",
            source_files=["auth/validator.py"],
            error_type="ValidationError"
        )

        fp2 = manager.compute_fingerprint(
            classification="client_misuse",  # Changed
            source_files=["auth/validator.py"],
            error_type="ValidationError"
        )

        assert fp1 != fp2

        # Different source file
        fp3 = manager.compute_fingerprint(
            classification="server_bug",
            source_files=["api/handler.py"],  # Changed
            error_type="ValidationError"
        )

        assert fp1 != fp3

    def test_extract_error_codes_from_title(self, temp_db):
        """Test extracting error codes from issue title for Tier 1 deduplication."""
        from code_indexer.server.self_monitoring.issue_manager import IssueManager

        manager = IssueManager(
            db_path=temp_db,
            scan_id="test-scan-006",
            github_repo="owner/repo"
        )

        # Test single error code
        codes = manager.extract_error_codes("[BUG] Authentication failed [AUTH-TOKEN-001]")
        assert codes == ["AUTH-TOKEN-001"]

        # Test multiple error codes
        codes = manager.extract_error_codes(
            "[BUG] Multiple errors [AUTH-TOKEN-001] [DB-CONN-002]"
        )
        assert codes == ["AUTH-TOKEN-001", "DB-CONN-002"]

        # Test no error codes
        codes = manager.extract_error_codes("[BUG] Generic error message")
        assert codes == []

    def test_get_existing_issues_metadata(self, temp_db):
        """Test retrieving existing issue metadata for deduplication."""
        from code_indexer.server.self_monitoring.issue_manager import IssueManager

        # Seed database with existing issues
        conn = sqlite3.connect(temp_db)
        conn.execute(
            "INSERT INTO self_monitoring_issues "
            "(scan_id, github_issue_number, github_issue_url, classification, "
            "error_codes, fingerprint, source_log_ids, source_files, title, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "scan-001",
                100,
                "https://github.com/owner/repo/issues/100",
                "server_bug",
                "AUTH-TOKEN-001",
                "abc123",
                "1,2,3",
                "auth/validator.py",
                "[BUG] Token validation [AUTH-TOKEN-001]",
                datetime.utcnow().isoformat()
            )
        )
        conn.execute(
            "INSERT INTO self_monitoring_issues "
            "(scan_id, github_issue_number, github_issue_url, classification, "
            "error_codes, fingerprint, source_log_ids, source_files, title, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                "scan-002",
                101,
                "https://github.com/owner/repo/issues/101",
                "client_misuse",
                "QUERY-PARAM-001",
                "def456",
                "4,5",
                "api/handler.py",
                "[CLIENT] Invalid parameter [QUERY-PARAM-001]",
                datetime.utcnow().isoformat()
            )
        )
        conn.commit()
        conn.close()

        manager = IssueManager(
            db_path=temp_db,
            scan_id="scan-003",
            github_repo="owner/repo"
        )

        # Retrieve metadata for last 90 days
        metadata = manager.get_existing_issues_metadata(days=90)

        assert len(metadata) == 2
        assert any(m["github_issue_number"] == 100 for m in metadata)
        assert any(m["github_issue_number"] == 101 for m in metadata)
        assert any(m["error_codes"] == "AUTH-TOKEN-001" for m in metadata)
        assert any(m["fingerprint"] == "abc123" for m in metadata)

    def test_create_issue_handles_multiple_error_codes(self, temp_db, mock_httpx_post):
        """Test IssueManager stores multiple error codes correctly."""
        from code_indexer.server.self_monitoring.issue_manager import IssueManager

        # Mock successful GitHub API response
        mock_httpx_post.return_value = Mock(
            status_code=201,
            json=lambda: {
                "number": 999,
                "html_url": "https://github.com/owner/repo/issues/999"
            },
            headers={},
            raise_for_status=lambda: None
        )

        manager = IssueManager(
            db_path=temp_db,
            scan_id="test-scan-008",
            github_repo="owner/repo",
            github_token="ghp_test_token"
        )

        manager.create_issue(
            classification="server_bug",
            title="[BUG] Multiple subsystem errors",
            body="## Description\nCascading failures...",
            source_log_ids=[8001, 8002],
            source_files=["auth/validator.py", "db/connection.py"],
            error_codes=["AUTH-TOKEN-001", "DB-CONN-002", "CACHE-MISS-003"]
        )

        # Verify multiple error codes stored
        conn = sqlite3.connect(temp_db)
        cursor = conn.execute(
            "SELECT error_codes FROM self_monitoring_issues WHERE github_issue_number = ?",
            (999,)
        )
        row = cursor.fetchone()
        conn.close()

        assert row is not None
        error_codes_str = row[0]
        # Should be comma-separated
        assert "AUTH-TOKEN-001" in error_codes_str
        assert "DB-CONN-002" in error_codes_str
        assert "CACHE-MISS-003" in error_codes_str

    def test_create_issue_prepends_server_identity_to_body(self, temp_db):
        """Test IssueManager prepends server identity to issue body when server_name provided (Bug #87 - Issue #4)."""
        from code_indexer.server.self_monitoring.issue_manager import IssueManager
        import socket

        # Track the JSON payload sent to GitHub API
        captured_body = None

        def mock_post_side_effect(*args, **kwargs):
            nonlocal captured_body
            # Capture the body from JSON payload
            if "json" in kwargs:
                captured_body = kwargs["json"].get("body")

            return Mock(
                status_code=201,
                json=lambda: {
                    "number": 777,
                    "html_url": "https://github.com/owner/repo/issues/777"
                },
                headers={},
                raise_for_status=lambda: None
            )

        with patch("code_indexer.server.self_monitoring.issue_manager.httpx.post", side_effect=mock_post_side_effect):
            # Create manager WITH server_name parameter
            manager = IssueManager(
                db_path=temp_db,
                scan_id="test-scan-010",
                github_repo="owner/repo",
                github_token="ghp_test_token",
                server_name="Production CIDX Server"
            )

            original_body = "## Problem\nDatabase connection timeout occurred."

            manager.create_issue(
                classification="server_bug",
                title="[BUG] DB timeout",
                body=original_body,
                source_log_ids=[10001],
                source_files=["db/connection.py"],
                error_codes=["DB-CONN-001"]
            )

        # Verify body was captured
        assert captured_body is not None

        # Verify server identity section is prepended
        assert "**Created by CIDX Server**" in captured_body
        assert "Production CIDX Server" in captured_body
        assert "test-scan-010" in captured_body
        assert socket.gethostbyname(socket.gethostname()) in captured_body or "Server IP:" in captured_body

        # Verify original body is still present after the identity section
        assert original_body in captured_body

        # Verify identity section comes before original body
        identity_pos = captured_body.find("**Created by CIDX Server**")
        original_pos = captured_body.find(original_body)
        assert identity_pos < original_pos

    def test_get_all_server_ips_returns_non_loopback_addresses(self, temp_db):
        """Test _get_all_server_ips() returns all non-loopback IPv4 addresses (Issue 1 fix)."""
        from code_indexer.server.self_monitoring.issue_manager import IssueManager

        manager = IssueManager(
            db_path=temp_db,
            scan_id="test-scan-011",
            github_repo="owner/repo",
            github_token="ghp_test_token"
        )

        # Call the helper method
        all_ips = manager._get_all_server_ips()

        # Verify result is a string
        assert isinstance(all_ips, str)

        # If we have IPs, verify they are non-loopback
        if all_ips and all_ips != "unknown":
            ips = all_ips.split(", ")
            # Should have at least one IP
            assert len(ips) >= 1
            # Should not include 127.0.0.1
            assert "127.0.0.1" not in ips
            # All should be valid IPv4 format
            for ip in ips:
                parts = ip.split(".")
                assert len(parts) == 4
                assert all(part.isdigit() and 0 <= int(part) <= 255 for part in parts)

    def test_create_issue_server_identity_not_duplicated(self, temp_db):
        """Test server identity section appears exactly once in issue body (Issue 2 fix)."""
        from code_indexer.server.self_monitoring.issue_manager import IssueManager

        # Track the JSON payload sent to GitHub API
        captured_body = None

        def mock_post_side_effect(*args, **kwargs):
            nonlocal captured_body
            if "json" in kwargs:
                captured_body = kwargs["json"].get("body")

            return Mock(
                status_code=201,
                json=lambda: {
                    "number": 888,
                    "html_url": "https://github.com/owner/repo/issues/888"
                },
                headers={},
                raise_for_status=lambda: None
            )

        with patch("code_indexer.server.self_monitoring.issue_manager.httpx.post", side_effect=mock_post_side_effect):
            manager = IssueManager(
                db_path=temp_db,
                scan_id="test-scan-012",
                github_repo="owner/repo",
                github_token="ghp_test_token",
                server_name="Test Server"
            )

            # Body that might be generated by Claude with its own identity section
            original_body = (
                "## Problem Description\n"
                "Authentication failures detected.\n\n"
                "## Analysis\n"
                "Token validation logic has a bug."
            )

            manager.create_issue(
                classification="server_bug",
                title="[BUG] Auth failure",
                body=original_body,
                source_log_ids=[12001],
                source_files=["auth/validator.py"],
                error_codes=["AUTH-001"]
            )

        # Verify body was captured
        assert captured_body is not None

        # Count occurrences of "Created by CIDX Server"
        identity_count = captured_body.count("**Created by CIDX Server**")
        assert identity_count == 1, f"Expected exactly 1 server identity section, found {identity_count}"

        # Verify Scan ID appears exactly once
        scan_id_count = captured_body.count("test-scan-012")
        assert scan_id_count == 1, f"Expected Scan ID once, found {scan_id_count} times"

        # Verify no duplicate "Server Name" or "Server IP" fields
        server_name_count = captured_body.count("Server Name:")
        server_ip_count = captured_body.count("Server IP:")
        assert server_name_count == 1, f"Expected 'Server Name:' once, found {server_name_count} times"
        assert server_ip_count == 1, f"Expected 'Server IP:' once, found {server_ip_count} times"
