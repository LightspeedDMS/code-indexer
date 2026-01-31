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
    def mock_subprocess(self):
        """Mock subprocess for gh CLI calls."""
        with patch("subprocess.run") as mock_run:
            yield mock_run

    def test_create_issue_calls_gh_cli(self, temp_db, mock_subprocess):
        """Test IssueManager calls gh CLI to create GitHub issue."""
        from code_indexer.server.self_monitoring.issue_manager import IssueManager

        # Mock successful gh CLI response
        mock_subprocess.return_value = Mock(
            returncode=0,
            stdout="https://github.com/owner/repo/issues/123\n",
            stderr=""
        )

        manager = IssueManager(
            db_path=temp_db,
            scan_id="test-scan-001",
            github_repo="owner/repo"
        )

        issue_data = manager.create_issue(
            classification="server_bug",
            title="[BUG] Authentication token validation failed",
            body="## Description\nToken validation error...",
            source_log_ids=[1001, 1002, 1003],
            source_files=["auth/token_validator.py"],
            error_codes=["AUTH-TOKEN-001"]
        )

        # Verify gh CLI was called correctly
        assert mock_subprocess.call_count >= 1
        args = mock_subprocess.call_args_list[0][0][0]
        assert "gh" in args
        assert "issue" in args
        assert "create" in args
        assert "--repo" in args
        assert "owner/repo" in args
        assert "--title" in args

        # Verify returned issue data
        assert issue_data["github_issue_number"] == 123
        assert issue_data["github_issue_url"] == "https://github.com/owner/repo/issues/123"
        assert issue_data["classification"] == "server_bug"

    def test_create_issue_stores_metadata_in_db(self, temp_db, mock_subprocess):
        """Test IssueManager stores issue metadata in SQLite database."""
        from code_indexer.server.self_monitoring.issue_manager import IssueManager

        # Mock successful gh CLI response
        mock_subprocess.return_value = Mock(
            returncode=0,
            stdout="https://github.com/owner/repo/issues/456\n",
            stderr=""
        )

        manager = IssueManager(
            db_path=temp_db,
            scan_id="test-scan-002",
            github_repo="owner/repo"
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

    def test_create_issue_handles_gh_cli_failure(self, temp_db, mock_subprocess):
        """Test IssueManager handles gh CLI failure gracefully."""
        from code_indexer.server.self_monitoring.issue_manager import IssueManager

        # Mock gh CLI failure
        mock_subprocess.return_value = Mock(
            returncode=1,
            stdout="",
            stderr="Error: API rate limit exceeded"
        )

        manager = IssueManager(
            db_path=temp_db,
            scan_id="test-scan-007",
            github_repo="owner/repo"
        )

        # Should raise exception on failure
        with pytest.raises(RuntimeError, match="rate limit"):
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

    def test_create_issue_computes_fingerprint(self, temp_db, mock_subprocess):
        """Test IssueManager computes deterministic fingerprint for deduplication."""
        from code_indexer.server.self_monitoring.issue_manager import IssueManager

        # Mock successful gh CLI response
        mock_subprocess.return_value = Mock(
            returncode=0,
            stdout="https://github.com/owner/repo/issues/789\n",
            stderr=""
        )

        manager = IssueManager(
            db_path=temp_db,
            scan_id="test-scan-003",
            github_repo="owner/repo"
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

    def test_fingerprint_is_deterministic(self, temp_db, mock_subprocess):
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

    def test_fingerprint_differs_for_different_inputs(self, temp_db, mock_subprocess):
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

    def test_extract_error_codes_from_title(self, temp_db, mock_subprocess):
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

    def test_get_existing_issues_metadata(self, temp_db, mock_subprocess):
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

    def test_create_issue_handles_multiple_error_codes(self, temp_db, mock_subprocess):
        """Test IssueManager stores multiple error codes correctly."""
        from code_indexer.server.self_monitoring.issue_manager import IssueManager

        # Mock successful gh CLI response
        mock_subprocess.return_value = Mock(
            returncode=0,
            stdout="https://github.com/owner/repo/issues/999\n",
            stderr=""
        )

        manager = IssueManager(
            db_path=temp_db,
            scan_id="test-scan-008",
            github_repo="owner/repo"
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

    def test_create_issue_sets_gh_token_env_var(self, temp_db, mock_subprocess):
        """Test IssueManager sets GH_TOKEN environment variable when token provided."""
        from code_indexer.server.self_monitoring.issue_manager import IssueManager

        # Mock successful gh CLI response
        mock_subprocess.return_value = Mock(
            returncode=0,
            stdout="https://github.com/owner/repo/issues/888\n",
            stderr=""
        )

        # Create manager WITH github_token parameter
        manager = IssueManager(
            db_path=temp_db,
            scan_id="test-scan-009",
            github_repo="owner/repo",
            github_token="ghp_test_token_123456789012345678901234"
        )

        manager.create_issue(
            classification="server_bug",
            title="[BUG] Test issue with token",
            body="Test body",
            source_log_ids=[9001],
            source_files=["test.py"],
            error_codes=[]
        )

        # Verify subprocess was called with GH_TOKEN in environment
        assert mock_subprocess.call_count >= 1
        call_kwargs = mock_subprocess.call_args_list[0][1]
        env = call_kwargs.get("env")

        assert env is not None
        assert "GH_TOKEN" in env
        assert env["GH_TOKEN"] == "ghp_test_token_123456789012345678901234"

    def test_create_issue_prepends_server_identity_to_body(self, temp_db):
        """Test IssueManager prepends server identity to issue body when server_name provided (Bug #87 - Issue #4)."""
        from code_indexer.server.self_monitoring.issue_manager import IssueManager
        import socket

        # Track the body content written to temp file
        captured_body = None

        def mock_run_side_effect(*args, **kwargs):
            nonlocal captured_body
            # Read body file before it gets deleted
            cmd_args = args[0]
            if "--body-file" in cmd_args:
                body_file_idx = cmd_args.index("--body-file") + 1
                body_file_path = cmd_args[body_file_idx]
                with open(body_file_path, 'r') as f:
                    captured_body = f.read()

            return Mock(
                returncode=0,
                stdout="https://github.com/owner/repo/issues/777\n",
                stderr=""
            )

        with patch("subprocess.run", side_effect=mock_run_side_effect):
            # Create manager WITH server_name parameter
            manager = IssueManager(
                db_path=temp_db,
                scan_id="test-scan-010",
                github_repo="owner/repo",
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
